from __future__ import absolute_import, print_function, division

from functools import reduce

import numpy
import pytest

from coffee.visitors import EstimateFlops

from ufl import (Mesh, FunctionSpace, FiniteElement, VectorElement,
                 TestFunction, TrialFunction, TensorProductCell, dx,
                 action, interval, quadrilateral, dot, grad)

from FIAT import ufc_cell
from FIAT.quadrature import GaussLobattoLegendreQuadratureLineRule

from finat.point_set import GaussLobattoLegendrePointSet
from finat.quadrature import QuadratureRule, TensorProductQuadratureRule

from tsfc import compile_form


def gll_quadrature_rule(cell, elem_deg):
    fiat_cell = ufc_cell("interval")
    fiat_rule = GaussLobattoLegendreQuadratureLineRule(fiat_cell, elem_deg + 1)
    line_rules = [QuadratureRule(GaussLobattoLegendrePointSet(fiat_rule.get_points()),
                                 fiat_rule.get_weights())
                  for _ in range(cell.topological_dimension())]
    finat_rule = reduce(lambda a, b: TensorProductQuadratureRule([a, b]), line_rules)
    return finat_rule


def mass_cg(cell, degree):
    m = Mesh(VectorElement('Q', cell, 1))
    V = FunctionSpace(m, FiniteElement('Q', cell, degree, variant='spectral'))
    u = TrialFunction(V)
    v = TestFunction(V)
    return u*v*dx(rule=gll_quadrature_rule(cell, degree))


def mass_dg(cell, degree):
    m = Mesh(VectorElement('Q', cell, 1))
    V = FunctionSpace(m, FiniteElement('DQ', cell, degree, variant='spectral'))
    u = TrialFunction(V)
    v = TestFunction(V)
    # In this case, the estimated quadrature degree will give the
    # correct number of quadrature points by luck.
    return u*v*dx


def laplace(cell, degree):
    m = Mesh(VectorElement('Q', cell, 1))
    V = FunctionSpace(m, FiniteElement('Q', cell, degree, variant='spectral'))
    u = TrialFunction(V)
    v = TestFunction(V)
    return dot(grad(u), grad(v))*dx(rule=gll_quadrature_rule(cell, degree))


def count_flops(form):
    kernel, = compile_form(form, parameters=dict(mode='spectral'))
    return EstimateFlops().visit(kernel.ast)


@pytest.mark.parametrize('form', [mass_cg, mass_dg])
@pytest.mark.parametrize(('cell', 'order'),
                         [(quadrilateral, 2),
                          (TensorProductCell(interval, interval), 2),
                          (TensorProductCell(quadrilateral, interval), 3)])
def test_mass(form, cell, order):
    degrees = numpy.arange(4, 10)
    flops = [count_flops(form(cell, int(degree))) for degree in degrees]
    rates = numpy.diff(numpy.log(flops)) / numpy.diff(numpy.log(degrees + 1))
    assert (rates < order).all()


@pytest.mark.parametrize('form', [mass_cg, mass_dg])
@pytest.mark.parametrize(('cell', 'order'),
                         [(quadrilateral, 2),
                          (TensorProductCell(interval, interval), 2),
                          (TensorProductCell(quadrilateral, interval), 3)])
def test_mass_action(form, cell, order):
    degrees = numpy.arange(4, 10)
    flops = [count_flops(action(form(cell, int(degree)))) for degree in degrees]
    rates = numpy.diff(numpy.log(flops)) / numpy.diff(numpy.log(degrees + 1))
    assert (rates < order).all()


@pytest.mark.parametrize(('cell', 'order'),
                         [(quadrilateral, 4),
                          (TensorProductCell(interval, interval), 4),
                          (TensorProductCell(quadrilateral, interval), 5)])
def test_laplace(cell, order):
    degrees = numpy.arange(4, 10)
    flops = [count_flops(laplace(cell, int(degree))) for degree in degrees]
    rates = numpy.diff(numpy.log(flops)) / numpy.diff(numpy.log(degrees + 1))
    assert (rates < order).all()


if __name__ == "__main__":
    import os
    import sys
    pytest.main(args=[os.path.abspath(__file__)] + sys.argv[1:])
