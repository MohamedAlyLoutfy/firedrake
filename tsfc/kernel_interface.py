from __future__ import absolute_import

import numpy

import coffee.base as coffee

import gem
from gem.node import traversal

from tsfc.finatinterface import create_element
from tsfc.coffee import SCALAR_TYPE


class Kernel(object):
    __slots__ = ("ast", "integral_type", "oriented", "subdomain_id",
                 "domain_number",
                 "coefficient_numbers", "__weakref__")
    """A compiled Kernel object.

    :kwarg ast: The COFFEE ast for the kernel.
    :kwarg integral_type: The type of integral.
    :kwarg oriented: Does the kernel require cell_orientations.
    :kwarg subdomain_id: What is the subdomain id for this kernel.
    :kwarg domain_number: Which domain number in the original form
        does this kernel correspond to (can be used to index into
        original_form.ufl_domains() to get the correct domain).
    :kwarg coefficient_numbers: A list of which coefficients from the
        form the kernel needs.
    """
    def __init__(self, ast=None, integral_type=None, oriented=False,
                 subdomain_id=None, domain_number=None,
                 coefficient_numbers=()):
        # Defaults
        self.ast = ast
        self.integral_type = integral_type
        self.oriented = oriented
        self.domain_number = domain_number
        self.subdomain_id = subdomain_id
        self.coefficient_numbers = coefficient_numbers
        super(Kernel, self).__init__()


class KernelBuilderBase(object):
    """Helper class for building local assembly kernels."""

    def __init__(self, interior_facet=False):
        """Initialise a kernel builder.

        :arg interior_facet: kernel accesses two cells
        """
        assert isinstance(interior_facet, bool)
        self.interior_facet = interior_facet

        # Coefficients
        self.coefficient_map = {}

        # Cell orientation
        if interior_facet:
            self._cell_orientations = gem.Variable("cell_orientations", (2, 1))
        else:
            self._cell_orientations = gem.Variable("cell_orientations", (1, 1))

    def coefficient(self, ufl_coefficient, restriction):
        """A function that maps :class:`ufl.Coefficient`s to GEM
        expressions."""
        kernel_arg = self.coefficient_map[ufl_coefficient]
        if ufl_coefficient.ufl_element().family() == 'Real':
            return kernel_arg
        else:
            return gem.partial_indexed(kernel_arg, {None: (), '+': (0,), '-': (1,)}[restriction])

    def cell_orientation(self, restriction):
        """Cell orientation as a GEM expression."""
        f = {None: 0, '+': 0, '-': 1}[restriction]
        co_int = gem.Indexed(self._cell_orientations, (f, 0))
        return gem.Conditional(gem.Comparison("==", co_int, gem.Literal(1)),
                               gem.Literal(-1),
                               gem.Conditional(gem.Comparison("==", co_int, gem.Zero()),
                                               gem.Literal(1),
                                               gem.Literal(numpy.nan)))

    def construct_kernel(self, name, args, body):
        """Construct a COFFEE function declaration with the
        accumulated glue code.

        :arg name: function name
        :arg args: function argument list
        :arg body: function body (:class:`coffee.Block` node)
        :returns: :class:`coffee.FunDecl` object
        """
        assert isinstance(body, coffee.Block)
        return coffee.FunDecl("void", name, args, body, pred=["static", "inline"])

    def arguments(self, arguments, indices):
        """Prepare arguments. Adds glue code for the arguments.

        :arg arguments: :class:`ufl.Argument`s
        :arg indices: GEM argument indices
        :returns: COFFEE function argument and GEM expression
                  representing the argument tensor
        """
        funarg, expressions = prepare_arguments(arguments, indices, interior_facet=self.interior_facet)
        return funarg, expressions

    def _coefficient(self, coefficient, name):
        """Prepare a coefficient. Adds glue code for the coefficient
        and adds the coefficient to the coefficient map.

        :arg coefficient: :class:`ufl.Coefficient`
        :arg name: coefficient name
        :returns: COFFEE function argument for the coefficient
        """
        funarg, expression = prepare_coefficient(coefficient, name, interior_facet=self.interior_facet)
        self.coefficient_map[coefficient] = expression
        return funarg


class KernelBuilder(KernelBuilderBase):
    """Helper class for building a :class:`Kernel` object."""

    def __init__(self, integral_type, subdomain_id, domain_number):
        """Initialise a kernel builder."""
        super(KernelBuilder, self).__init__(integral_type.startswith("interior_facet"))

        self.kernel = Kernel(integral_type=integral_type, subdomain_id=subdomain_id,
                             domain_number=domain_number)
        self.local_tensor = None
        self.coordinates_arg = None
        self.coefficient_args = []
        self.coefficient_split = {}

        # Facet number
        if integral_type in ['exterior_facet', 'exterior_facet_vert']:
            facet = gem.Variable('facet', (1,))
            self._facet_number = {None: gem.VariableIndex(gem.Indexed(facet, (0,)))}
        elif integral_type in ['interior_facet', 'interior_facet_vert']:
            facet = gem.Variable('facet', (2,))
            self._facet_number = {
                '+': gem.VariableIndex(gem.Indexed(facet, (0,))),
                '-': gem.VariableIndex(gem.Indexed(facet, (1,)))
            }
        elif integral_type == 'interior_facet_horiz':
            self._facet_number = {'+': 1, '-': 0}

    def facet_number(self, restriction):
        """Facet number as a GEM index."""
        return self._facet_number[restriction]

    def set_arguments(self, arguments, indices):
        """Process arguments.

        :arg arguments: :class:`ufl.Argument`s
        :arg indices: GEM argument indices
        :returns: GEM expression representing the return variable
        """
        self.local_tensor, expressions = self.arguments(arguments, indices)
        return expressions

    def set_coordinates(self, coefficient, name):
        """Prepare the coordinate field.

        :arg coefficient: :class:`ufl.Coefficient`
        :arg name: coordinate coefficient name
        """
        self.coordinates_arg = self._coefficient(coefficient, name)

    def set_coefficients(self, integral_data, form_data):
        """Prepare the coefficients of the form.

        :arg integral_data: UFL integral data
        :arg form_data: UFL form data
        """
        from ufl import Coefficient, MixedElement as ufl_MixedElement, FunctionSpace
        coefficients = []
        coefficient_numbers = []
        # enabled_coefficients is a boolean array that indicates which
        # of reduced_coefficients the integral requires.
        for i in range(len(integral_data.enabled_coefficients)):
            if integral_data.enabled_coefficients[i]:
                coefficient = form_data.reduced_coefficients[i]
                if type(coefficient.ufl_element()) == ufl_MixedElement:
                    split = [Coefficient(FunctionSpace(coefficient.ufl_domain(), element))
                             for element in coefficient.ufl_element().sub_elements()]
                    coefficients.extend(split)
                    self.coefficient_split[coefficient] = split
                else:
                    coefficients.append(coefficient)
                # This is which coefficient in the original form the
                # current coefficient is.
                # Consider f*v*dx + g*v*ds, the full form contains two
                # coefficients, but each integral only requires one.
                coefficient_numbers.append(form_data.original_coefficient_positions[i])
        for i, coefficient in enumerate(coefficients):
            self.coefficient_args.append(
                self._coefficient(coefficient, "w_%d" % i))
        self.kernel.coefficient_numbers = tuple(coefficient_numbers)

    def require_cell_orientations(self):
        """Set that the kernel requires cell orientations."""
        self.kernel.oriented = True

    def construct_kernel(self, name, body):
        """Construct a fully built :class:`Kernel`.

        This function contains the logic for building the argument
        list for assembly kernels.

        :arg name: function name
        :arg body: function body (:class:`coffee.Block` node)
        :returns: :class:`Kernel` object
        """
        args = [self.local_tensor, self.coordinates_arg]
        if self.kernel.oriented:
            args.append(cell_orientations_coffee_arg)
        args.extend(self.coefficient_args)
        if self.kernel.integral_type in ["exterior_facet", "exterior_facet_vert"]:
            args.append(coffee.Decl("unsigned int",
                                    coffee.Symbol("facet", rank=(1,)),
                                    qualifiers=["const"]))
        elif self.kernel.integral_type in ["interior_facet", "interior_facet_vert"]:
            args.append(coffee.Decl("unsigned int",
                                    coffee.Symbol("facet", rank=(2,)),
                                    qualifiers=["const"]))

        self.kernel.ast = KernelBuilderBase.construct_kernel(self, name, args, body)
        return self.kernel


def prepare_coefficient(coefficient, name, interior_facet=False):
    """Bridges the kernel interface and the GEM abstraction for
    Coefficients.

    :arg coefficient: UFL Coefficient
    :arg name: unique name to refer to the Coefficient in the kernel
    :arg interior_facet: interior facet integral?
    :returns: (funarg, expression)
         funarg     - :class:`coffee.Decl` function argument
         expression - GEM expression referring to the Coefficient
                      values
    """
    assert isinstance(interior_facet, bool)

    if coefficient.ufl_element().family() == 'Real':
        # Constant
        funarg = coffee.Decl(SCALAR_TYPE, coffee.Symbol(name),
                             pointers=[("restrict",)],
                             qualifiers=["const"])

        expression = gem.reshape(gem.Variable(name, (None,)),
                                 coefficient.ufl_shape)

        return funarg, expression

    finat_element = create_element(coefficient.ufl_element())

    # TODO: move behind the FInAT interface!
    if hasattr(finat_element, '_base_element'):
        scalar_shape = finat_element._base_element.index_shape
        tensor_shape = finat_element.index_shape[len(scalar_shape):]
    else:
        scalar_shape = finat_element.index_shape
        tensor_shape = ()

    if interior_facet:
        scalar_shape = (2,) + scalar_shape

    funarg = coffee.Decl(SCALAR_TYPE, coffee.Symbol(name),
                         pointers=[("const", "restrict"), ("restrict",)],
                         qualifiers=["const"])

    scalar_size = numpy.prod(scalar_shape)
    tensor_size = numpy.prod(tensor_shape)
    expression = gem.reshape(
        gem.Variable(name, (scalar_size, tensor_size)),
        scalar_shape, tensor_shape
    )

    return funarg, expression


def prepare_arguments(arguments, indices, interior_facet=False):
    """Bridges the kernel interface and the GEM abstraction for
    Arguments.  Vector Arguments are rearranged here for interior
    facet integrals.

    :arg arguments: UFL Arguments
    :arg indices: Argument indices
    :arg interior_facet: interior facet integral?
    :returns: (funarg, expression)
         funarg      - :class:`coffee.Decl` function argument
         expressions - GEM expressions referring to the argument
                       tensor
    """
    from itertools import product
    assert isinstance(interior_facet, bool)

    if len(arguments) == 0:
        # No arguments
        funarg = coffee.Decl(SCALAR_TYPE, coffee.Symbol("A", rank=(1,)))
        expression = gem.Indexed(gem.Variable("A", (1,)), (0,))

        return funarg, [expression]

    elements = tuple(create_element(arg.ufl_element()) for arg in arguments)
    shapes = tuple(element.index_shape for element in elements)

    c_shape = numpy.array([numpy.prod(shape) for shape in shapes])
    if interior_facet:
        c_shape *= 2
    c_shape = tuple(c_shape)

    funarg = coffee.Decl(SCALAR_TYPE, coffee.Symbol("A", rank=c_shape))
    varexp = gem.Variable("A", c_shape)

    if not interior_facet:
        expression = gem.FlexiblyIndexed(
            varexp,
            tuple((0, tuple(zip(alpha, shape)))
                  for shape, alpha in zip(shapes, indices))
        )
        return funarg, [expression]
    else:
        expressions = []
        for restrictions in product((0, 1), repeat=len(arguments)):
            expressions.append(gem.FlexiblyIndexed(
                varexp,
                tuple((0, ((r, 2),) + tuple(zip(alpha, shape)))
                      for r, shape, alpha in zip(restrictions, shapes, indices))
            ))
        return funarg, expressions


def needs_cell_orientations(ir):
    """Does a multi-root GEM expression DAG references cell
    orientations?"""
    for node in traversal(ir):
        if isinstance(node, gem.Variable) and node.name == "cell_orientations":
            return True
    return False


cell_orientations_coffee_arg = coffee.Decl("int", coffee.Symbol("cell_orientations"),
                                           pointers=[("restrict",), ("restrict",)],
                                           qualifiers=["const"])
"""COFFEE function argument for cell orientations"""
