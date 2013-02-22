import sys
from numba.minivect import minitypes
from . import sreg, smem, barrier, macros
import numpy as np
# modify numba behavior

from numba import utils, functions, llvm_types
from numba import nodes
from numba import pipeline
import ast
from numba import type_inference
from numba.symtab import Variable
from .nvvm import ADDRSPACE_SHARED
from llvm.core import *
from numba.intrinsic import default_intrinsic_library
from numba.external import default_external_library
from numba.codegen.llvmcontext import LLVMContextManager
from numba.codegen import translate

import logging

logger = logging.getLogger(__name__)

class CudaAttributeNode(nodes.Node):
    _attributes = ['value']
    
    def __init__(self, value):
        self.value = value
    
    def resolve(self, name):
        return type(self)(getattr(self.value, name))
    
    def __repr__(self):
        return '<%s value=%s>' % (type(self).__name__, self.value)

class CudaSMemArrayNode(nodes.Node):
    pass

class CudaSMemArrayCallNode(nodes.Node):
    _attributes = ('shape', 'variable')
    def __init__(self, context, shape, dtype):
        self.shape = shape
        tmp_strides = [dtype.itemsize]
        for s in reversed(self.shape[1:]):
            tmp_strides.append(tmp_strides[-1] * s)
        self.strides = tuple(reversed(tmp_strides))

        self.elemcount = np.prod(self.shape)
        self.dtype = dtype
        type = minitypes.ArrayType(dtype=dtype,
                                   ndim=len(self.shape),
                                   is_c_contig=True)

        self.variable = Variable(type, promotable_type=False)


class CudaSMemAssignNode(nodes.Node):
    
    _fields = ['target', 'value']
    
    def __init__(self, target, value):
        self.target = target
        self.value = value

class CudaMacroGridNode(nodes.Node):
    pass

class CudaMacroGridExpandValuesNode(nodes.Node):
    pass

def _make_sreg_call(attr):
    fname = sreg.SPECIAL_VALUES[attr]
    sig = minitypes.FunctionType(minitypes.uint32, [])
    funcnode = nodes.LLVMExternalFunctionNode(sig, fname)
    callnode = nodes.NativeFunctionCallNode(sig, funcnode, [])
    return callnode

def _make_sreg_pattern(x, y, z):
    x, y, z = (_make_sreg_call(i) for i in [x, y, z])
    mul = ast.BinOp(op=ast.Mult(), left=y, right=z)
    add = ast.BinOp(op=ast.Add(), left=x, right =mul)
    return add


class CudaAttrRewriteMixin(object):

    def visit_Attribute(self, node):
        from numbapro import cuda as _THIS_MODULE
        
        value = self.visit(node.value)
        retval = node # default to return the original node
        
        if isinstance(node.value, ast.Name):
            #assert isinstance(value.ctx, ast.Load)
            obj = self.func.func_globals.get(node.value.id)
            if obj is _THIS_MODULE:
                retval = CudaAttributeNode(_THIS_MODULE).resolve(node.attr)
        elif isinstance(value, CudaAttributeNode):
            retval = value.resolve(node.attr)
        
        if retval.value in sreg.SPECIAL_VALUES:  # sreg
            # subsitute with a function call 
            sig = minitypes.FunctionType(minitypes.uint32, [])
            fname = sreg.SPECIAL_VALUES[retval.value]
            funcnode = nodes.LLVMExternalFunctionNode(sig, fname)
            callnode = nodes.NativeFunctionCallNode(sig, funcnode, [])
            retval = callnode
        elif retval.value == smem._array:   # allocate shared memory
            retval = CudaSMemArrayNode()
        elif retval.value == barrier.syncthreads: # syncthreads
            sig = minitypes.FunctionType(minitypes.void, [])
            fname = 'llvm.nvvm.barrier0'
            funcnode = nodes.LLVMExternalFunctionNode(sig, fname)
            retval = funcnode
        elif retval.value == macros.grid:  # expand into sreg attributes
            retval = CudaMacroGridNode()
        if retval is node:
            retval = super(CudaAttrRewriteMixin, self).visit_Attribute(node)
        
        return retval

    def visit_Call(self, node):
        func = self.visit(node.func)
        if isinstance(func, CudaSMemArrayNode):
            assert len(node.args) <= 2
            kws = dict((kw.arg, kw.value)for kw in node.keywords)
            
            arglist = 'shape', 'dtype'
            for i, v in enumerate(node.args):
                k = arglist[i]
                if k in kws:
                    raise KeyError("%s is re-defined as keyword argument" % k)
                else:
                    kws[k] = v
        
            shape = tuple()
        
            for elem in kws['shape'].elts:
                node = self.visit(elem)
                shape += (node.pyval,)
    
            dtype_id = kws['dtype'].id # FIXME must be a ast.Name
            dtype = self.func.func_globals[dtype_id] # FIXME must be a Numba type
        
            node = CudaSMemArrayCallNode(self.context, shape=shape, dtype=dtype)
            return node
        elif isinstance(func, nodes.LLVMExternalFunctionNode):
            self.visitlist(node.args)
            self.visitlist(node.keywords)
            callnode = nodes.NativeFunctionCallNode(func.signature, func,
                                                    node.args)
            return callnode
        elif isinstance(func, CudaMacroGridNode):
            assert len(node.args) == 1
            assert len(node.keywords) == 0
            ndim = self.visit(node.args[0]).pyval
            if ndim == 1:
                node = _make_sreg_pattern(sreg.threadIdx.x,
                                        sreg.blockIdx.x,
                                        sreg.blockDim.x)
                return self.visit(node)
            elif ndim == 2:
                node1 = _make_sreg_pattern(sreg.threadIdx.x,
                                           sreg.blockIdx.x,
                                           sreg.blockDim.x)
                node2 = _make_sreg_pattern(sreg.threadIdx.y,
                                           sreg.blockIdx.y,
                                           sreg.blockDim.y)

                return self.visit(ast.Tuple(elts=[node1, node2],
                                            ctx=ast.Load()))
            else:
                raise ValueError("Dimension is only valid for 1 or 2, " \
                                 "but got %d" % ndim)
        else:
            return super(CudaAttrRewriteMixin, self).visit_Call(node)

    def visit_Assign(self, node):
        node.inplace_op = getattr(node, 'inplace_op', None)
        node.value = self.visit(node.value)

        if isinstance(node.value, CudaSMemArrayCallNode):
            errmsg = "LHS of shared memory declaration can have only one value."
            assert len(node.targets) == 1, errmsg
            target = node.targets[0] = self.visit(node.targets[0])
            node = CudaSMemAssignNode(node.targets[0], node.value)
            self.assign(target, node.value)
            return node

        # FIXME: the following is copied from TypeInferer.visit_Assign
        #        there seems to be some side-effect in visit(node.value)
        if len(node.targets) != 1 or isinstance(node.targets[0], (ast.List,
                                                                  ast.Tuple)):
            return self._handle_unpacking(node)

        target = node.targets[0] = self.visit(node.targets[0])
        self.assign(target, node.value)

        lhs_var = target.variable
        rhs_var = node.value.variable
        if isinstance(target, ast.Name):
            node.value = nodes.CoercionNode(node.value, lhs_var.type)
        elif lhs_var.type != rhs_var.type:
            if lhs_var.type.is_array and rhs_var.type.is_array:
                # Let other code handle array coercions
                pass
            else:
                node.value = nodes.CoercionNode(node.value, lhs_var.type)
        
        return node


class CudaTypeInferer(CudaAttrRewriteMixin,
                      type_inference.infer.TypeInferer):
    pass

class CudaCodeGenerator(translate.LLVMCodeGenerator):
    def __init__(self, *args, **kws):
        super(CudaCodeGenerator, self).__init__(*args, **kws)
        self.__smem = {}

    def visit_CudaSMemArrayCallNode(self, node):
        from numba.ndarray_helpers import PyArrayAccessor
        ndarray_ptr_ty = node.variable.ltype
        ndarray_ty = ndarray_ptr_ty.pointee
        ndarray = self.builder.alloca(ndarray_ty)
        
        accessor = PyArrayAccessor(self.builder, ndarray)

        # store ndim
        store = lambda src, dst: self.builder.store(src, dst)
        accessor.ndim = Constant.int(llvm_types._int32, len(node.shape))
        
        
        # store data
        mod = self.builder.basic_block.function.module
        smem_elemtype = node.dtype.to_llvm(self.context)
        smem_type = Type.array(smem_elemtype, node.elemcount)
        smem = mod.add_global_variable(smem_type, 'smem', ADDRSPACE_SHARED)
        smem.initializer = Constant.undef(smem_type)

        smem_elem_ptr_ty = Type.pointer(smem_elemtype)
        smem_elem_ptr_ty_addrspace = Type.pointer(smem_elemtype,
                                                  ADDRSPACE_SHARED)
        smem_elem_ptr = smem.bitcast(smem_elem_ptr_ty_addrspace)
        tyname = str(smem_elemtype)
        tyname = {'float': 'f32', 'double': 'f64'}.get(tyname, tyname)
        s2g_intrinic = 'llvm.nvvm.ptr.shared.to.gen.p0%s.p3%s' % (tyname, tyname)
        shared_to_generic = mod.get_or_insert_function(
                                    Type.function(smem_elem_ptr_ty,
                                                  [smem_elem_ptr_ty_addrspace]),
                                   s2g_intrinic)
        
        data = self.builder.call(shared_to_generic, [smem_elem_ptr])
        accessor.data = self.builder.bitcast(data, llvm_types._void_star)
        
        # store dims
        intp_t = llvm_types._intp
        const_intp = lambda x: Constant.int(intp_t, x)
        const_int = lambda x: Constant.int(Type.int(), x)
        
        dims = self.builder.alloca_array(intp_t,
                                         Constant.int(Type.int(),
                                                      len(node.shape)))
        
        for i, s in enumerate(node.shape):
            ptr = self.builder.gep(dims, map(const_int, [i]))
            store(const_intp(s), ptr)
        
        accessor.dims = dims
                
        # store strides
        strides = self.builder.alloca_array(intp_t,
                                            Constant.int(Type.int(),
                                                         len(node.strides)))

        
        for i, s in enumerate(node.strides):
            ptr = self.builder.gep(strides, map(const_int, [i]))
            store(const_intp(s), ptr)

        accessor.strides = strides
    
        return ndarray

    def visit_Name(self, node):
        try:
            return self.__smem[node.id]
        except KeyError:
            return super(CudaCodeGenerator, self).visit_Name(node)

    def visit_CudaSMemAssignNode(self, node):
        self.__smem[node.target.id] = self.visit(node.value)

class NumbaproCudaPipeline(pipeline.Pipeline):
#    def __init__(self, context, func, node, func_signature, **kwargs):
#        super(NumbaproCudaPipeline, self).__init__(context, func, node,
#                                                   func_signature, **kwargs)
#        self.insert_specializer('rewrite_cuda_sreg', after='type_infer')
#    
#    def rewrite_cuda_sreg(self, node):
#        return CudaSRegRewrite(self.context, self.func, node).visit(node)

    order = [
             'const_folding',
             'cfg',
             #'dump_cfg',
             'type_infer',
             'type_set',
             'dump_cfg',
             # 'closure_type_inference', # not supported
             'transform_for',
             'specialize',
             'late_specializer',
             'fix_ast_locations',
             'cleanup_symtab',
             'codegen',
             ]

    def __init__(self, *args, **kwargs):
        kwargs['order'] = kwargs.get('order', NumbaproCudaPipeline.order)
        super(NumbaproCudaPipeline, self).__init__(*args, **kwargs)

    def make_specializer(self, cls, ast, **kwds):
        self.mixins = {}
        return super(NumbaproCudaPipeline, self).make_specializer(cls, ast,
                                                                  **kwds)

    def type_infer(self, ast):
        type_inferer = self.make_specializer(CudaTypeInferer, ast,
                                             **self.kwargs)
        type_inferer.infer_types()

        self.func_signature = type_inferer.func_signature
        logger.debug("signature for %s: %s", self.mangled_name,
                     self.func_signature)
        self.symtab = type_inferer.symtab
        if self.func.__name__ == 'mean_reduce':
            from numba.utils import dump
            dump(ast)
        return ast

    def codegen(self, ast):
        self.translator = self.make_specializer(CudaCodeGenerator,
                                                ast, **self.kwargs)
        self.translator.translate()
        return ast

context = utils.get_minivect_context()
context.llvm_context = LLVMContextManager()
context.numba_pipeline = NumbaproCudaPipeline
function_cache = context.function_cache = functions.FunctionCache(context)
context.intrinsic_library = default_intrinsic_library(context)
context.external_library = default_external_library(context)