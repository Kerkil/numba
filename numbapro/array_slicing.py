import ast

import llvm.core
# Temporary fix for utilty function defintions to merge during linkage.
# TODO: make intrinsic functions out of these utilities
from llvm.core import LINKAGE_LINKONCE_ODR
from llvm_cbuilder import *
from llvm_cbuilder import shortnames as C

from numba import *
from numba import nodes, ndarray_helpers, llvm_types
from numba.minivect import minitypes
from numbapro.vectorize.gufunc import PyArray

class SliceDimNode(nodes.ExprNode):
    """
    Array is sliced, and this dimension contains an integer index or newaxis.
    """

    _fields = ['subslice']

    def __init__(self, subslice, src_dim, dst_dim, **kwargs):
        super(SliceDimNode, self).__init__(**kwargs)
        self.subslice = subslice
        self.src_dim = src_dim
        self.dst_dim = dst_dim
        self.type = subslice.type

        # PyArrayAccessor wrapper of llvm fake PyArrayObject value
        # set by NativeSliceNode
        self.view_accessor = None
        self.view_copy_accessor = None

class SliceSliceNode(SliceDimNode):
    """
    Array is sliced, and this dimension contains a slice.
    """

    _fields = ['start', 'stop', 'step']

    def __init__(self, subslice, src_dim, dst_dim, **kwargs):
        super(SliceSliceNode, self).__init__(subslice, src_dim, dst_dim,
                                             **kwargs)
        self.start = subslice.lower and nodes.CoercionNode(subslice.lower, npy_intp)
        self.stop = subslice.upper and nodes.CoercionNode(subslice.upper, npy_intp)
        self.step = subslice.step and nodes.CoercionNode(subslice.step, npy_intp)

class BroadcastNode(nodes.ExprNode):
    """
    Broadcast a bunch of operands:

        - set strides of single-sized dimensions to zero
        - find big shape
    """

    _fields = ['operands', 'check_errors']

    def __init__(self, array_type, operands, **kwargs):
        super(BroadcastNode, self).__init__(**kwargs)
        self.operands = operands

        self.shape_type = minitypes.CArrayType(npy_intp, array_type.ndim)
        self.array_type = array_type
        self.type = npy_intp.pointer()

        self.broadcast_retvals = {}
        self.check_errors = []

        for op in operands:
            if op.type.is_array:
                # TODO: Put the raise code in a separate basic block and jump
                return_value = nodes.LLVMValueRefNode(int_, None)
                check_error = nodes.CheckErrorNode(
                        return_value, 0, exc_type=ValueError,
                        exc_msg="Shape mismatch while broadcasting")

                self.broadcast_retvals[op] = return_value
                self.check_errors.append(check_error)

def create_slice_dim_node(subslice, *args):
    if subslice.type.is_slice:
        return SliceSliceNode(subslice, *args)
    else:
        return SliceDimNode(subslice, *args)

class NativeSliceNode(nodes.ExprNode):
    """
    Aggregate of slices in all dimensions.

    In nopython context, uses a fake stack-allocated PyArray struct.

    In python context, it builds an actual heap-allocated numpy array.
    In this case, the following attributes are patched during code generation
    time that sets the llvm values:

        dst_data, dst_shape, dst_strides
    """

    _fields = ['value', 'subslices', 'build_array_node']

    def __init__(self, type, value, subslices, nopython, **kwargs):
        super(NativeSliceNode, self).__init__(**kwargs)
        value = nodes.CloneableNode(value)

        self.type = type
        self.value = value
        self.subslices = subslices

        self.shape_type = minitypes.CArrayType(npy_intp, type.ndim)
        self.nopython = nopython
        if not nopython:
            self.build_array_node = self.build_array()
        else:
            self.build_array_node = None

    def mark_nopython(self):
        self.nopython = True
        self.build_array_node = None

    def build_array(self):
        self.dst_data = nodes.LLVMValueRefNode(void.pointer(), None)
        self.dst_shape = nodes.LLVMValueRefNode(self.shape_type, None)
        self.dst_strides = nodes.LLVMValueRefNode(self.shape_type, None)
        array_node = nodes.ArrayNewNode(
                self.type, self.dst_data, self.dst_shape, self.dst_strides,
                base=self.value.clone)
        return nodes.CoercionNode(array_node, self.type)


def rewrite_slice(node, nopython):
    """
    Rewrites array slices to its native equivalent without
    using the Python API.

        node:       ast.Subscript with an array type as result
        nopython:   whether the node is encountered in a nopython context
    """
    # assert self.nopython

    if isinstance(node.slice, ast.ExtSlice):
        dims = node.slice.dims
    else:
        assert not isinstance(node.slice, ast.Ellipsis)
        dims = [node.slice]

    slices = []
    src_dim = 0
    dst_dim = 0

    all_slices = True
    for subslice in dims:
        slices.append(create_slice_dim_node(subslice, src_dim, dst_dim))

        if subslice.type.is_slice:
            src_dim += 1
            dst_dim += 1
        elif nodes.is_newaxis(subslice):
            all_slices = False
            dst_dim += 1
        else:
            assert subslice.type.is_int
            all_slices = False
            src_dim += 1

    #if all_slices and all(empty(subslice) for subslice in slices):
    #    return node.value

    # print node, node.type
    return NativeSliceNode(node.type, node.value, slices, nopython)


class MarkNoPython(ast.NodeVisitor):
    """
    Mark array slicing nodes as nopython, which allows them to use
    stack-allocated fake arrays.
    """

    def visit_NativeSliceNode(self, node):
        node.mark_nopython()
        self.generic_visit(node)
        return node

def mark_nopython(ast):
    MarkNoPython().visit(ast)

class FakePyArrayAccessor(object):
    pass

class NativeSliceCodegenMixin(object): # ast_translate.LLVMCodeGenerator):

    def __init__(self, *args, **kwds):
        super(NativeSliceCodegenMixin, self).__init__(*args, **kwds)

        newaxis_func_def = NewAxis()
        self.newaxis_func = newaxis_func_def(self.llvm_module)
        self.newaxis_func.linkage = LINKAGE_LINKONCE_ODR

        index_func_def = IndexAxis()
        self.index_func = index_func_def(self.llvm_module)
        self.index_func.linkage = LINKAGE_LINKONCE_ODR

    def visit_NativeSliceNode(self, node):
        """
        Slice an array. Allocate fake PyArray and allocate shape/strides
        """
        array_ltype = PyArray.llvm_type()
        shape_ltype = npy_intp.pointer().to_llvm(self.context)

        # Create PyArrayObject accessors
        view = self.visit(node.value)
        view_accessor = ndarray_helpers.PyArrayAccessor(self.builder, view)

        if node.nopython:
            view_copy = self.llvm_alloca(array_ltype)
            self.builder.store(self.builder.load(view), view_copy)
            view_copy_accessor = ndarray_helpers.PyArrayAccessor(self.builder,
                                                                 view_copy)
        else:
            view_copy_accessor = FakePyArrayAccessor()

        # Stack-allocate shape/strides and update accessors
        shape = self.alloca(node.shape_type)
        strides = self.alloca(node.shape_type)

        view_copy_accessor.data = view_accessor.data
        view_copy_accessor.shape = self.builder.bitcast(shape, shape_ltype)
        view_copy_accessor.strides = self.builder.bitcast(strides, shape_ltype)

        # Patch and visit all children
        for subslice in node.subslices:
            subslice.view_accessor = view_accessor
            subslice.view_copy_accessor = view_copy_accessor

        # print ast.dump(node)
        self.visitlist(node.subslices)

        # Return fake or actual array
        if node.nopython:
            return view_copy
        else:
            # Update LLVMValueRefNode fields, build actual numpy array
            void_p = void.pointer().to_llvm(self.context)
            node.dst_data.llvm_value = self.builder.bitcast(
                                    view_copy_accessor.data, void_p)
            node.dst_shape.llvm_value = view_copy_accessor.shape
            node.dst_strides.llvm_value = view_copy_accessor.strides
            return self.visit(node.build_array_node)

    def visit_SliceSliceNode(self, node):
        "Handle slicing"
        start, stop, step = node.start, node.stop, node.step

        if start is not None:
            start = self.visit(node.start)
        if stop is not None:
            stop = self.visit(node.stop)
        if step is not None:
            step = self.visit(node.step)

        slice_func_def = SliceArray(self.context,
                                    start is not None,
                                    stop is not None,
                                    step is not None)

        slice_func = slice_func_def(self.llvm_module)
        slice_func.linkage = LINKAGE_LINKONCE_ODR

        data = node.view_copy_accessor.data
        in_shape = node.view_accessor.shape
        in_strides = node.view_accessor.strides
        out_shape = node.view_copy_accessor.shape
        out_strides = node.view_copy_accessor.strides
        src_dim = llvm_types.constant_int(node.src_dim)
        dst_dim = llvm_types.constant_int(node.dst_dim)

        default = llvm_types.constant_int(0, C.npy_intp)
        args = [data, in_shape, in_strides, out_shape, out_strides,
                start or default, stop or default, step or default,
                src_dim, dst_dim]
        data_p = self.builder.call(slice_func, args)
        node.view_copy_accessor.data = data_p

        return None

    def visit_SliceDimNode(self, node):
        "Handle indexing and newaxes in a slice operation"
        acc_copy = node.view_copy_accessor
        acc = node.view_accessor
        if node.type.is_int:
            value = self.visit(nodes.CoercionNode(node.subslice, npy_intp))
            args = [acc_copy.data, acc.shape, acc.strides,
                    llvm_types.constant_int(node.src_dim, C.npy_intp), value]
            result = self.builder.call(self.index_func, args)
            acc_copy.data = result
        else:
            args = [acc_copy.shape, acc_copy.strides,
                    llvm_types.constant_int(node.dst_dim)]
            self.builder.call(self.newaxis_func, args)

        return None

    def visit_BroadcastNode(self, node):
        shape = self.alloca(node.shape_type)
        shape = self.builder.bitcast(shape, node.type.to_llvm(self.context))

        # Initialize shape to ones
        default_extent = llvm.core.Constant.int(C.npy_intp, 1)
        for i in range(node.array_type.ndim):
            dst = self.builder.gep(shape, [llvm.core.Constant.int(C.int, i)])
            self.builder.store(default_extent, dst)

        func_def = Broadcast()
        broadcast = func_def(self.llvm_module)
        broadcast.linkage = LINKAGE_LINKONCE_ODR

        for op in node.operands:
            op_result = self.visit(op)
            acc = ndarray_helpers.PyArrayAccessor(self.builder, op_result)
            if op.type.is_array:
                args = [shape, acc.shape, acc.strides,
                        llvm_types.constant_int(node.array_type.ndim),
                        llvm_types.constant_int(op.type.ndim)]
                lresult = self.builder.call(broadcast, args)
                node.broadcast_retvals[op].llvm_value = lresult

        self.visitlist(node.check_errors)

        return shape

class ConstantBase(CDefinition):

    def get_constants(self):
        zero = self.constant(C.npy_intp, 0)
        one = self.constant(C.npy_intp, 1)
        return one, zero

class SliceArray(ConstantBase):

    _name_ = "slice"
    _retty_ = C.char_p
    _argtys_ = [
        ('data', C.char_p),

        ('in_shape', C.pointer(C.npy_intp)),
        ('in_strides', C.pointer(C.npy_intp)),

        ('out_shape', C.pointer(C.npy_intp)),
        ('out_strides', C.pointer(C.npy_intp)),

        ('start', C.npy_intp),
        ('stop', C.npy_intp),
        ('step', C.npy_intp),

        ('src_dim', C.int),
        ('dst_dim', C.int),
    ]

    def _adjust_given_index(self, extent, negative_step, index, is_start):
        # Tranliterate the below code to llvm cbuilder

        # For the start index in start:stop:step, do:
        # if have_start:
        #     if start < 0:
        #         start += shape
        #         if start < 0:
        #             start = 0
        #     elif start >= shape:
        #         if negative_step:
        #             start = shape - 1
        #         else:
        #             start = shape
        # else:
        #     if negative_step:
        #         start = shape - 1
        #     else:
        #         start = 0

        # For the stop index, do:
        # if stop is not None:
        #     if stop < 0:
        #         stop += extent
        #         if stop < 0:
        #             stop = 0
        #     elif stop > extent:
        #         stop = extent
        # else:
        #     if negative_step:
        #         stop = -1
        #     else:
        #         stop = extent

        one, zero = self.get_constants()

        with self.ifelse(index < zero) as ifelse:
            with ifelse.then():
                index += extent
                with self.ifelse(index < zero) as ifelse_inner:
                    with ifelse_inner.then():
                        index.assign(zero)

            with ifelse.otherwise():
                with self.ifelse(index >= extent) as ifelse:
                    with ifelse.then():
                        if is_start:
                            # index is 'start' index
                            with self.ifelse(negative_step) as ifelse:
                                with ifelse.then():
                                    index.assign(extent - one)
                                with ifelse.otherwise():
                                    index.assign(extent)
                        else:
                            # index is 'stop' index. Stop is exclusive, to
                            # we don't care about the sign of the step
                            index.assign(extent)

    def _set_default_index(self, default1, default2, negative_step, index):
        with self.ifelse(negative_step) as ifelse:
            with ifelse.then():
                index.assign(default1)
            with ifelse.otherwise():
                index.assign(default2)

    def adjust_index(self, extent, negative_step, index, default1, default2,
                     is_start=False, have_index=True):
        if have_index:
            self._adjust_given_index(extent, negative_step, index, is_start)
        else:
            self._set_default_index(default1, default2, negative_step, index)

    def body(self, data, in_shape, in_strides, out_shape, out_strides,
             start, stop, step, src_dim, dst_dim):

        stride = in_strides[src_dim]
        extent = in_shape[src_dim]

        one, zero = self.get_constants()
        if not self.have_step:
            step = one

        negative_step = step < zero

        self.adjust_index(extent, negative_step, start,
                                  default1=extent - one, default2=zero,
                                  is_start=True, have_index=self.have_start)
        self.adjust_index(extent, negative_step, stop,
                                 default1=-one, default2=extent,
                                 have_index=self.have_stop)

        # self.debug("extent", extent)
        # self.debug("negative_step", negative_step.cast(C.npy_intp))
        # self.debug("start/stop/step", start, stop, step)
        new_extent = self.var(C.npy_intp)
        new_extent.assign((stop - start) / step)
        with self.ifelse((stop - start) % step != zero) as ifelse:
            with ifelse.then():
                new_extent += one

        with self.ifelse(new_extent < zero) as ifelse:
            with ifelse.then():
                new_extent.assign(zero)

        # if extent == 1, set stride to 0 for broadcasting
        with self.ifelse(new_extent == one) as ifelse:
            with ifelse.then():
                stride.assign(zero)

        result = self.var(data.type, name='result')
        result.assign(data[start * stride:])
        out_shape[dst_dim] = new_extent
        # self.debug("new_extent", new_extent)
        out_strides[dst_dim] = stride * step

        self.ret(result)

    def specialize(self, context, have_start, have_stop, have_step):
        self.context = context

        self.have_start = have_start
        self.have_stop = have_stop
        self.have_step = have_step

        self._name_ = "slice_%s_%s_%s" % (have_start, have_stop, have_step)

class IndexAxis(ConstantBase):

    _name_ = "index"
    _retty_ = C.char_p
    _argtys_ = [
        ('data', C.char_p),
        ('in_shape', C.pointer(C.npy_intp)),
        ('in_strides', C.pointer(C.npy_intp)),
        ('src_dim', C.npy_intp),
        ('index', C.npy_intp),
    ]

    def body(self, data, in_shape, in_strides, src_dim, index):
        result = self.var(data.type, name='result')
        # self.debug("indexing...", src_dim, "stride", in_strides[src_dim])
        result.assign(data[in_strides[src_dim] * index:])
        self.ret(result)

#    def specialize(self):
#        self.specialize_name()

class NewAxis(ConstantBase):

    _name_ = "newaxis"
    _argtys_ = [
        ('out_shape', C.pointer(C.npy_intp)),
        ('out_strides', C.pointer(C.npy_intp)),
        ('dst_dim', C.int),
    ]

    def body(self, out_shape, out_strides, dst_dim):
        one, zero = self.get_constants()
        out_shape[dst_dim] = one
        out_strides[dst_dim] = zero
        # self.debug("newaxis in dimension:", dst_dim)
        self.ret()

#    def specialize(self):
#        self.specialize_name()

class Broadcast(ConstantBase):
    """
    Transliteration of

        @cname('__pyx_memoryview_broadcast')
        cdef bint __pyx_broadcast(Py_ssize_t *dst_shape,
                                  Py_ssize_t *input_shape,
                                  Py_ssize_t *strides,
                                  int max_ndim, int ndim,
                                  bint *p_broadcast) nogil except -1:
            cdef Py_ssize_t i
            cdef int dim_offset = max_ndim - ndim

            for i in range(ndim):
                src_extent = input_shape[i]
                dst_extent = dst_shape[i + dim_offset]

                if src_extent == 1:
                    p_broadcast[0] = True
                    strides[i] = 0
                elif dst_extent == 1:
                    dst_shape[i + dim_offset] = src_extent
                elif src_extent != dst_extent:
                    __pyx_err_extents(i, dst_shape[i], input_shape[i])
    """

    _name_ = "broadcast"
    _argtys_ = [
        ('dst_shape', C.pointer(C.npy_intp)),
        ('src_shape', C.pointer(C.npy_intp)),
        ('src_strides', C.pointer(C.npy_intp)),
        ('max_ndim', C.int),
        ('ndim', C.int),
    ]
    _retty_ = C.int

    def body(self, dst_shape, src_shape, src_strides, max_ndim, ndim):
        dim_offset = max_ndim - ndim

        def constants(type):
            return self.constant(type, 0), self.constant(type, 1)

        zero, one = constants(C.npy_intp)
        zero_int, one_int = constants(C.int)

        with self.for_range(ndim) as (loop, i):
            src_extent = src_shape[i]
            dst_extent = dst_shape[i + dim_offset]

            with self.ifelse(src_extent == one) as ifelse:
                with ifelse.then():
                    # p_broadcast[0] = True
                    src_strides[i] = zero
                with ifelse.otherwise():
                    with self.ifelse(dst_extent == one) as ifelse:
                        with ifelse.then():
                            dst_shape[i + dim_offset] = src_extent

                        with ifelse.otherwise():
                            with self.ifelse(src_extent != dst_extent) as ifelse:
                                with ifelse.then():
                                    # Shape mismatch
                                    self.ret(zero_int)

        self.ret(one_int)

#    def specialize(self):
#        self.specialize_name()