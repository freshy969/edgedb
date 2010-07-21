##
# Copyright (c) 2008-2010 Sprymix Inc.
# All rights reserved.
#
# See LICENSE for details.
##


import collections
import itertools

from semantix.caos import name as caos_name
from semantix.caos import error as caos_error
from semantix.caos import utils as caos_utils
from semantix.caos.utils import LinearPath, MultiPath
from semantix.caos import types as caos_types
from semantix.caos.tree import ast as caos_ast

from semantix.utils.algos import boolean
from semantix.utils import datastructures, ast, debug
from semantix.utils.functional import checktypes


class PathIndex(dict):
    """
    Graph path mapping path identifiers to AST nodes
    """

    def update(self, other):
        for k, v in other.items():
            if k in self:
                super().__getitem__(k).update(v)
            else:
                self[k] = v

    def __setitem__(self,  key, value):
        if not isinstance(key, (LinearPath, str)):
            raise TypeError('Invalid key type for PathIndex: %s' % key)

        if not isinstance(value, set):
            value = {value}

        super().__setitem__(key, value)

    """
    def __getitem__(self, key):
        result = set()
        for k, v in self.items():
            if k == key:
                result.update(v)
        if not result:
            raise KeyError
        return result
    """

    """
    def __contains__(self, key):
        for k in self.keys():
            if k == key:
                return True
        return False
    """


class TreeError(Exception):
    pass


@checktypes
class TreeTransformer:

    def extract_prefixes(self, expr, prefixes=None):
        prefixes = prefixes if prefixes is not None else PathIndex()

        if isinstance(expr, caos_ast.PathCombination):
            for path in expr.paths:
                self.extract_prefixes(path, prefixes)

        elif isinstance(expr, (caos_ast.EntitySet, caos_ast.AtomicRefSimple)):
            key = getattr(expr, 'anchor', None) or expr.id

            if key:
                # XXX AtomicRefs with PathCombinations in ref don't have an id
                if key not in prefixes:
                    prefixes[key] = {expr}
                else:
                    prefixes[key].add(expr)

            if isinstance(expr, caos_ast.EntitySet) and expr.rlink:
                self.extract_prefixes(expr.rlink.source, prefixes)
            elif isinstance(expr, caos_ast.AtomicRefSimple):
                self.extract_prefixes(expr.ref, prefixes)

        elif isinstance(expr, caos_ast.EntityLink):
            self.extract_prefixes(expr.target or expr.source, prefixes)

        elif isinstance(expr, caos_ast.LinkPropRefSimple):
            self.extract_prefixes(expr.ref, prefixes)

        elif isinstance(expr, caos_ast.BinOp):
            self.extract_prefixes(expr.left, prefixes)
            self.extract_prefixes(expr.right, prefixes)

        elif isinstance(expr, caos_ast.UnaryOp):
            self.extract_prefixes(expr.expr, prefixes)

        elif isinstance(expr, (caos_ast.InlineFilter, caos_ast.InlinePropFilter)):
            self.extract_prefixes(expr.ref, prefixes)
            self.extract_prefixes(expr.expr, prefixes)

        elif isinstance(expr, (caos_ast.AtomicRefExpr, caos_ast.LinkPropRefExpr)):
            self.extract_prefixes(expr.expr, prefixes)

        elif isinstance(expr, caos_ast.FunctionCall):
            for arg in expr.args:
                self.extract_prefixes(arg, prefixes)

        elif isinstance(expr, caos_ast.TypeCast):
            self.extract_prefixes(expr.expr, prefixes)

        elif isinstance(expr, caos_ast.NoneTest):
            self.extract_prefixes(expr.expr, prefixes)

        elif isinstance(expr, (caos_ast.Sequence, caos_ast.Record)):
            for path in expr.elements:
                self.extract_prefixes(path, prefixes)

        elif isinstance(expr, caos_ast.Constant):
            pass

        elif isinstance(expr, caos_ast.GraphExpr):
            pass
            """
            if expr.generator:
                self.extract_prefixes(expr.generator)

            if expr.selector:
                for e in expr.selector:
                    self.extract_prefixes(e.expr, prefixes)

            if expr.grouper:
                for e in expr.grouper:
                    self.extract_prefixes(e, prefixes)

            if expr.sorter:
                for e in expr.sorter:
                    self.extract_prefixes(e, prefixes)
            """

        else:
            assert False, 'unexpected node: "%r"' % expr

        return prefixes

    def replace_atom_refs(self, expr, prefixes):

        if isinstance(expr, caos_ast.AtomicRefSimple):
            arefs = (expr,)
        else:
            arefs = ast.find_children(expr, lambda i: isinstance(i, caos_ast.AtomicRefSimple))

        for aref in arefs:
            if isinstance(aref.ref, caos_ast.PathCombination):
                aref_prefixes = [getattr(r, 'anchor', None) or r.id for r in aref.ref.paths]
            else:
                aref_prefixes = [getattr(aref.ref, 'anchor', None) or aref.ref.id]

            newrefs = set()

            for prefix in aref_prefixes:
                newref = prefixes.get(prefix)

                if not newref:
                    continue

                if aref.id:
                    # XXX: not all AtomRefs have a valid id, i.e the ones that have
                    # Disjunction as their ref.
                    for ref in newref:
                        # Make sure to pull the atom from all the alternative paths.
                        # atomrefs might have fallen out of date due to development of
                        # alternative paths by the path merger.
                        #
                        if isinstance(aref, caos_ast.MetaRef):
                            ref.metarefs.update(prefixes[aref.id])
                        else:
                            ref.atomrefs.update(prefixes[aref.id])

                newrefs.update(newref)

            if newrefs:
                if len(newrefs) > 1:
                    newrefs = caos_ast.Disjunction(paths=frozenset(newrefs))
                else:
                    newrefs = next(iter(newrefs))
                aref.ref = newrefs

        return expr

    def add_path_user(self, path, user):
        while path:
            path.users.add(user)
            if path.rlink:
                path.rlink.users.add(user)
                path = path.rlink.source
            else:
                path = None
        return path

    def entityref_to_idref(self, expr, schema, full_record=False):
        p = next(iter(expr.paths))
        if isinstance(p, caos_ast.EntitySet):
            concepts = {c.concept for c in expr.paths}
            assert len(concepts) == 1

            elements = []

            concept = p.concept
            ref = p if len(expr.paths) == 1 else expr

            if full_record:
                for link_name, link in concept.pointers.items():
                    if link.atomic():
                        link_proto = schema.get(link_name)
                        target_proto = link_proto.target
                        id = LinearPath(ref.id)
                        id.add(link_proto, caos_types.OutboundDirection, target_proto)
                        elements.append(caos_ast.AtomicRefSimple(ref=ref, name=link_name, id=id))

                metaref = caos_ast.MetaRef(name='id', ref=ref)

                for p in expr.paths:
                    p.atomrefs.update(elements)
                    p.metarefs.add(metaref)

                elements.append(metaref)

                expr = caos_ast.Record(elements=elements, concept=concept)
            else:
                link_name = caos_name.Name('semantix.caos.builtins.id')
                link_proto = schema.get(link_name)
                target_proto = link_proto.target
                id = LinearPath(ref.id)
                id.add(link_proto, caos_types.OutboundDirection, target_proto)
                expr = caos_ast.AtomicRefSimple(ref=ref, name=link_name, id=id)

        return expr

    def _dump(self, tree):
        if tree is not None:
            print(tree.dump(pretty=True, colorize=True, width=180, field_mask='^(_.*|refs|backrefs)$'))
        else:
            print('None')

    def extend_binop(self, binop, *exprs, op=ast.ops.AND, reversed=False):
        exprs = list(exprs)
        binop = binop or exprs.pop(0)

        for expr in exprs:
            if expr is not binop:
                if reversed:
                    binop = caos_ast.BinOp(right=binop, op=op, left=expr)
                else:
                    binop = caos_ast.BinOp(left=binop, op=op, right=expr)

        return binop

    def is_aggregated_expr(self, expr, deep=False):
        agg = getattr(expr, 'aggregates', False) or \
                   (isinstance(expr, caos_types.NodeClass) and \
                        caos_utils.get_path_id(expr) in self.context.current.groupprefixes)

        if not agg and deep:
            return bool(list(ast.find_children(expr, lambda i: getattr(i, 'aggregates', None))))
        return agg

    def reorder_aggregates(self, expr):
        if getattr(expr, 'aggregates', False):
            # No need to drill-down, the expression is known to be a pure aggregate
            return expr

        if isinstance(expr, caos_ast.FunctionCall):
            has_agg_args = False

            for arg in expr.args:
                self.reorder_aggregates(arg)

                if self.is_aggregated_expr(arg):
                    has_agg_args = True
                elif has_agg_args and not isinstance(expr, caos_ast.Constant):
                    raise TreeError('invalid expression mix of aggregates and non-aggregates')

            if has_agg_args:
                expr.aggregates = True

        elif isinstance(expr, caos_ast.BinOp):
            left = self.reorder_aggregates(expr.left)
            right = self.reorder_aggregates(expr.right)

            left_aggregates = self.is_aggregated_expr(left)
            right_aggregates = self.is_aggregated_expr(right)

            if (left_aggregates and (right_aggregates or isinstance(right, caos_ast.Constant))) \
               or (isinstance(left, caos_ast.Constant) and right_aggregates):
                expr.aggregates = True

            elif expr.op == ast.ops.AND:
                if right_aggregates:
                    # Reorder the operands so that aggregate expr is always on the left
                    expr.left, expr.right = expr.right, expr.left

            elif left_aggregates or right_aggregates:
                raise TreeError('invalid expression mix of aggregates and non-aggregates')

        elif isinstance(expr, caos_ast.UnaryOp):
            self.reorder_aggregates(expr.expr)

        elif isinstance(expr, caos_ast.NoneTest):
            self.reorder_aggregates(expr.expr)

        elif isinstance(expr, (caos_ast.AtomicRef, caos_ast.Constant, caos_ast.InlineFilter,
                               caos_ast.EntitySet, caos_ast.InlinePropFilter)):
            pass

        elif isinstance(expr, caos_ast.PathCombination):
            for p in expr.paths:
                self.reorder_aggregates(p)

        elif isinstance(expr, (caos_ast.Sequence, caos_ast.Record)):
            has_agg_elems = False
            for item in expr.elements:
                self.reorder_aggregates(item)
                if self.is_aggregated_expr(item):
                    has_agg_elems = True
                elif has_agg_elems and not isinstance(expr, caos_ast.Constant):
                    raise TreeError('invalid expression mix of aggregates and non-aggregates')

            if has_agg_elems:
                expr.aggregates = True

        elif isinstance(expr, caos_ast.GraphExpr):
            pass

        else:
            # All other nodes fall through
            assert False, 'unexpected node "%r"' % expr

        return expr


    def link_subqueries(self, expr, paths):
        subpaths = self.extract_paths(expr, reverse=True, resolve_arefs=False,
                                      recurse_subqueries=True)

        if not isinstance(subpaths, caos_ast.EntitySet):
            self.flatten_path_combination(subpaths, recursive=True)
            subpaths = subpaths.paths
        else:
            subpaths = {subpaths}

        for subpath in subpaths:
            outer = paths.getlist(subpath.id)
            if outer and subpath not in outer:
                subpath.reference = outer[0]


    def postprocess_expr(self, expr):
        paths = self.extract_paths(expr, reverse=True)

        if isinstance(paths, caos_ast.PathCombination):
            paths = paths.paths
        else:
            paths = {paths}

        for path in paths:
            self._postprocess_expr(path)

    def _postprocess_expr(self, expr):
        if isinstance(expr, caos_ast.EntitySet):
            if self.context.current.location == 'generator':
                if len(expr.disjunction.paths) == 1 and len(expr.conjunction.paths) == 0:
                    # Generator by default produces strong paths, that must limit every other
                    # path in the query.  However, to accommodate for possible disjunctions
                    # in generator expressions, links are put into disjunction.  If, in fact,
                    # there was not disjunctive expressions in generator, the link must
                    # be turned into conjunction.
                    #
                    expr.conjunction = caos_ast.Conjunction(paths=expr.disjunction.paths)
                    expr.disjunction = caos_ast.Disjunction()

            for path in expr.conjunction.paths:
                self._postprocess_expr(path)

            for path in expr.disjunction.paths:
                self._postprocess_expr(path)

        elif isinstance(expr, caos_ast.PathCombination):
            for path in expr.paths:
                self._postprocess_expr(path)

        elif isinstance(expr, caos_ast.EntityLink):
            if expr.target:
                self._postprocess_expr(expr.target)

        else:
            assert False, "Unexpexted expression: %s" % expr

    def is_weak_op(self, op):
        return op in (ast.ops.OR, ast.ops.IN, ast.ops.NOT_IN) or \
               self.context.current.location != 'generator'

    def merge_paths(self, expr):
        if isinstance(expr, caos_ast.AtomicRefExpr):
            if self.context.current.location == 'generator':
                expr.ref.filter = self.extend_binop(expr.ref.filter, expr.expr)
                self.merge_paths(expr.ref)
                expr = caos_ast.InlineFilter(expr=expr.ref.filter, ref=expr.ref)
            else:
                self.merge_paths(expr.expr)

        elif isinstance(expr, caos_ast.LinkPropRefExpr):
            if self.context.current.location == 'generator':
                expr.ref.propfilter = self.extend_binop(expr.ref.propfilter, expr.expr)
                if expr.ref.target:
                    self.merge_paths(expr.ref.target)
                else:
                    self.merge_paths(expr.ref.source)
                expr = caos_ast.InlinePropFilter(expr=expr.ref.propfilter, ref=expr.ref)
            else:
                self.merge_paths(expr.expr)

        elif isinstance(expr, caos_ast.BinOp):
            left = self.merge_paths(expr.left)
            right = self.merge_paths(expr.right)

            if self.is_weak_op(expr.op):
                combination = caos_ast.Disjunction
            else:
                combination = caos_ast.Conjunction

            paths = set()
            for operand in (left, right):
                if isinstance(operand, (caos_ast.InlineFilter, caos_ast.AtomicRefSimple)):
                    paths.add(operand.ref)
                else:
                    paths.add(operand)

            e = combination(paths=frozenset(paths))
            merge_filters = self.context.current.location != 'generator'
            self.flatten_and_unify_path_combination(e, deep=False, merge_filters=merge_filters)

            if len(e.paths) > 1:
                expr = caos_ast.BinOp(left=left, op=expr.op, right=right, aggregates=expr.aggregates)
            else:
                expr = next(iter(e.paths))

        elif isinstance(expr, caos_ast.UnaryOp):
            expr.expr = self.merge_paths(expr.expr)

        elif isinstance(expr, caos_ast.TypeCast):
            expr.expr = self.merge_paths(expr.expr)

        elif isinstance(expr, caos_ast.NoneTest):
            expr.expr = self.merge_paths(expr.expr)

        elif isinstance(expr, caos_ast.PathCombination):
            expr = self.flatten_and_unify_path_combination(expr, deep=True)

        elif isinstance(expr, caos_ast.MetaRef):
            expr.ref.metarefs.add(expr)

        elif isinstance(expr, caos_ast.AtomicRefSimple):
            expr.ref.atomrefs.add(expr)

        elif isinstance(expr, caos_ast.LinkPropRefSimple):
            expr.ref.proprefs.add(expr)

        elif isinstance(expr, caos_ast.EntitySet):
            if expr.rlink:
                self.merge_paths(expr.rlink.source)

        elif isinstance(expr, caos_ast.EntityLink):
            if expr.source:
                self.merge_paths(expr.source)

        elif isinstance(expr, (caos_ast.InlineFilter, caos_ast.Constant, caos_ast.InlinePropFilter)):
            pass

        elif isinstance(expr, caos_ast.FunctionCall):
            args = []
            for arg in expr.args:
                args.append(self.merge_paths(arg))
            expr = expr.__class__(name=expr.name, args=args, aggregates=expr.aggregates)

        elif isinstance(expr, (caos_ast.Sequence, caos_ast.Record)):
            elements = []
            for element in expr.elements:
                elements.append(self.merge_paths(element))

            if isinstance(expr, caos_ast.Record):
                expr = expr.__class__(elements=elements, concept=expr.concept)
            else:
                expr = expr.__class__(elements=elements)

        elif isinstance(expr, caos_ast.GraphExpr):
            pass

        else:
            assert False, 'unexpected node "%r"' % expr

        return expr

    def flatten_path_combination(self, expr, recursive=False):
        paths = set()
        for path in expr.paths:
            if isinstance(path, expr.__class__) or \
                        (recursive and isinstance(path, caos_ast.PathCombination)):
                if recursive:
                    path.update(self.flatten_path_combination(path, recursive=True))
                else:
                    paths.update(path.paths)
            else:
                paths.add(path)

        expr.paths = frozenset(paths)
        return expr

    def flatten_and_unify_path_combination(self, expr, deep=False, merge_filters=False):
        ##
        # Flatten nested disjunctions and conjunctions since they are associative
        #
        assert isinstance(expr, caos_ast.PathCombination)

        self.flatten_path_combination(expr)

        if deep:
            newpaths = set()
            for path in expr.paths:
                path = self.merge_paths(path)
                newpaths.add(path)

            expr = expr.__class__(paths=frozenset(newpaths))

        self.unify_paths(expr.paths, mode=expr.__class__, merge_filters=merge_filters)

        expr.paths = frozenset(p for p in expr.paths)
        return expr

    nest = 0

    @debug.debug
    def unify_paths(self, paths, mode, reverse=True, merge_filters=False):
        mypaths = set(paths)

        result = None

        while mypaths and not result:
            result = self.extract_paths(mypaths.pop(), reverse)

        while mypaths:
            path = self.extract_paths(mypaths.pop(), reverse)

            if not path:
                continue

            if issubclass(mode, caos_ast.Disjunction):
                """LOG [caos.graph.merge] ADDING
                print(' ' * self.nest, 'ADDING', result, path, getattr(result, 'id', '??'), getattr(path, 'id', '??'), merge_filters)
                self.nest += 2
                """

                result = self.add_paths(result, path, merge_filters=merge_filters)
                assert result

                """LOG [caos.graph.merge] ADDITION RESULT
                self.nest -= 2
                if not self.nest:
                    self._dump(result)
                """
            else:
                """LOG [caos.graph.merge] INTERSECTING
                print(' ' * self.nest, result, path, getattr(result, 'id', '??'), getattr(path, 'id', '??'), merge_filters)
                self.nest += 2
                """

                result = self.intersect_paths(result, path, merge_filters=merge_filters)
                assert result

                """LOG [caos.graph.merge] INTERSECTION RESULT
                self._dump(result)
                self.nest -= 2
                """

        return result

    def miniterms_from_conjunctions(self, paths):
        variables = datastructures.OrderedSet()

        terms = []

        for path in paths:
            term = 0

            if isinstance(path, caos_ast.Conjunction):
                for subpath in path.paths:
                    if subpath not in variables:
                        variables.add(subpath)
                    term += 1 << variables.index(subpath)

            elif isinstance(path, caos_ast.EntityLink):
                if path not in variables:
                    variables.add(path)
                term += 1 << variables.index(path)

            terms.append(term)

        return variables, boolean.ints_to_terms(*terms)

    def conjunctions_from_miniterms(self, terms, variables):
        paths = set()

        for term in terms:
            conjpaths = [variables[i] for i, bit in enumerate(term) if bit]
            if len(conjpaths) > 1:
                paths.add(caos_ast.Conjunction(paths=frozenset(conjpaths)))
            else:
                paths.add(conjpaths[0])
        return paths

    def minimize_disjunction(self, paths):
        variables, miniterms = self.miniterms_from_conjunctions(paths)
        minimized = boolean.minimize(miniterms)
        paths = self.conjunctions_from_miniterms(minimized, variables)
        result = caos_ast.Disjunction(paths=frozenset(paths))
        return result

    def add_sets(self, left, right, merge_filters=False):
        if left is right:
            return left

        match = self.match_prefixes(left, right, ignore_filters=merge_filters)
        if match:
            if isinstance(left, caos_ast.EntityLink):
                left_link = left
                right_link = right

                left = left.target
                right = right.target
            else:
                left_link = left.rlink
                right_link = right.rlink

            if left_link:
                self.fixup_refs([right_link], left_link)
                if merge_filters and right_link.propfilter:
                    left_link.propfilter = self.extend_binop(left_link.propfilter,
                                                             right_link.propfilter, op=ast.ops.AND)

                left_link.proprefs.update(right_link.proprefs)
                left_link.users.update(right_link.users)
                if right_link.target:
                    left_link.target = right_link.target

            if left and right:
                self.fixup_refs([right], left)

                if merge_filters and right.filter:
                    left.filter = self.extend_binop(left.filter, right.filter, op=ast.ops.AND)

                if merge_filters:
                    paths_left = set()
                    for dpath in right.disjunction.paths:
                        if isinstance(dpath, (caos_ast.EntitySet, caos_ast.EntityLink)):
                            merged = self.intersect_paths(left.conjunction, dpath, merge_filters)
                            if merged is not left.conjunction:
                                paths_left.add(dpath)
                        else:
                            paths_left.add(dpath)
                    right.disjunction = caos_ast.Disjunction(paths=frozenset(paths_left))

                left.disjunction = self.add_paths(left.disjunction,
                                                  right.disjunction, merge_filters)
                left.atomrefs.update(right.atomrefs)
                left.metarefs.update(right.metarefs)
                left.users.update(right.users)
                left.joins.update(right.joins)
                left.joins.discard(left)

                if merge_filters:
                    left.conceptfilter.update(right.conceptfilter)

                if merge_filters:
                    left.conjunction = self.intersect_paths(left.conjunction,
                                                            right.conjunction, merge_filters)

                    # If greedy disjunction merging is requested, we must also try to
                    # merge disjunctions.
                    paths = frozenset(left.conjunction.paths) | frozenset(left.disjunction.paths)
                    self.unify_paths(paths, caos_ast.Conjunction, reverse=False, merge_filters=True)
                    left.disjunction.paths = left.disjunction.paths - left.conjunction.paths
                else:
                    conjunction = self.add_paths(left.conjunction, right.conjunction, merge_filters)
                    if conjunction.paths:
                        left.disjunction.update(conjunction)
                    left.conjunction.paths = frozenset()

            if isinstance(left, caos_ast.EntitySet):
                return left
            elif isinstance(right, caos_ast.EntitySet):
                return right
            else:
                return left_link
        else:
            result = caos_ast.Disjunction(paths=frozenset((left, right)))

        return result

    def add_to_disjunction(self, disjunction, path, merge_filters):
        # Other operand is a disjunction -- look for path we can merge with,
        # if not found, append to disjunction.
        for dpath in disjunction.paths:
            if isinstance(dpath, (caos_ast.EntityLink, caos_ast.EntitySet)):
                merge = self.add_sets(dpath, path, merge_filters)
                if merge is dpath:
                    break
        else:
            disjunction.update(path)

        return disjunction

    def add_to_conjunction(self, conjunction, path, merge_filters):
        result = None
        if merge_filters:
            for cpath in conjunction.paths:
                if isinstance(cpath, (caos_ast.EntityLink, caos_ast.EntitySet)):
                    merge = self.add_sets(cpath, path, merge_filters)
                    if merge is cpath:
                        result = conjunction
                        break

        if not result:
            result = caos_ast.Disjunction(paths=frozenset({conjunction, path}))

        return result

    def add_disjunctions(self, left, right, merge_filters=False):
        result = caos_ast.Disjunction()
        result.update(left)
        result.update(right)

        if len(result.paths) > 1:
            self.unify_paths(result.paths, mode=result.__class__, reverse=False,
                             merge_filters=merge_filters)
            result.paths = frozenset(p for p in result.paths)

        return result

    def add_conjunction_to_disjunction(self, disjunction, conjunction):
        if disjunction.paths and conjunction.paths:
            return caos_ast.Disjunction(paths=frozenset({disjunction, conjunction}))
        elif disjunction.paths:
            return disjunction
        elif conjunction.paths:
            return caos_ast.Disjunction(paths=frozenset({conjunction}))
        else:
            return caos_ast.Disjunction()

    def add_conjunctions(self, left, right):
        paths = frozenset(p for p in (left, right) if p.paths)
        return caos_ast.Disjunction(paths=paths)

    def add_paths(self, left, right, merge_filters=False):
        if isinstance(left, (caos_ast.EntityLink, caos_ast.EntitySet)):
            if isinstance(right, (caos_ast.EntityLink, caos_ast.EntitySet)):
                # Both operands are sets -- simply merge them
                result = self.add_sets(left, right, merge_filters)

            elif isinstance(right, caos_ast.Disjunction):
                result = self.add_to_disjunction(right, left, merge_filters)

            elif isinstance(right, caos_ast.Conjunction):
                result = self.add_to_conjunction(right, left, merge_filters)

        elif isinstance(left, caos_ast.Disjunction):
            if isinstance(right, (caos_ast.EntityLink, caos_ast.EntitySet)):
                result = self.add_to_disjunction(left, right, merge_filters)

            elif isinstance(right, caos_ast.Disjunction):
                result = self.add_disjunctions(left, right, merge_filters)

            elif isinstance(right, caos_ast.Conjunction):
                result = self.add_conjunction_to_disjunction(left, right)

        elif isinstance(left, caos_ast.Conjunction):
            if isinstance(right, (caos_ast.EntityLink, caos_ast.EntitySet)):
                result = self.add_to_conjunction(left, right, merge_filters)

            elif isinstance(right, caos_ast.Disjunction):
                result = self.add_conjunction_to_disjunction(right, left)

            elif isinstance(right, caos_ast.Conjunction):
                result = self.add_conjunctions(left, right)

        return result


    def intersect_sets(self, left, right, merge_filters=False):
        if left is right:
            return left

        match = self.match_prefixes(left, right, ignore_filters=True)
        if match:
            if isinstance(left, caos_ast.EntityLink):
                left_set = left.target
                right_set = right.target
                left_link = left
                right_link = right
            else:
                left_set = left
                right_set = right
                left_link = left.rlink
                right_link = right.rlink

            if left_link:
                self.fixup_refs([right_link], left_link)
                if right_link.propfilter:
                    left_link.propfilter = self.extend_binop(left_link.propfilter,
                                                             right_link.propfilter, op=ast.ops.AND)

                left_link.proprefs.update(right_link.proprefs)
                left_link.users.update(right_link.users)
                if right_link.target:
                    left_link.target = right_link.target

            if right_set and left_set:
                self.fixup_refs([right_set], left_set)

                if right_set.filter:
                    left_set.filter = self.extend_binop(left_set.filter, right_set.filter,
                                                        op=ast.ops.AND)

                left_set.conjunction = self.intersect_paths(left_set.conjunction,
                                                            right_set.conjunction, merge_filters)
                left_set.atomrefs.update(right_set.atomrefs)
                left_set.metarefs.update(right_set.metarefs)
                left_set.users.update(right_set.users)
                left_set.joins.update(right_set.joins)
                left_set.joins.discard(left_set)
                left_set.conceptfilter.update(right_set.conceptfilter)

                disjunction = self.intersect_paths(left_set.disjunction,
                                                   right_set.disjunction, merge_filters)

                left_set.disjunction = caos_ast.Disjunction()

                if isinstance(disjunction, caos_ast.Disjunction):
                    left_set.disjunction = disjunction

                    if len(left_set.disjunction.paths) == 1:
                        first_disj = next(iter(left_set.disjunction.paths))
                        if isinstance(first_disj, caos_ast.Conjunction):
                            left_set.conjunction = first_disj
                            left_set.disjunction = caos_ast.Disjunction()

                elif disjunction.paths:
                    left_set.conjunction = self.intersect_paths(left_set.conjunction,
                                                                disjunction, merge_filters)

                    self.flatten_path_combination(left_set.conjunction)

                    if len(left_set.conjunction.paths) == 1:
                        first_conj = next(iter(left_set.conjunction.paths))
                        if isinstance(first_conj, caos_ast.Disjunction):
                            left_set.disjunction = first_conj
                            left_set.conjunction = caos_ast.Conjunction()

            if isinstance(left, caos_ast.EntitySet):
                return left
            elif isinstance(right, caos_ast.EntitySet):
                return right
            else:
                return left_link

        else:
            result = caos_ast.Conjunction(paths=frozenset({left, right}))

        return result

    def intersect_with_disjunction(self, disjunction, path):
        result = caos_ast.Conjunction(paths=frozenset((disjunction, path)))
        return result

    def intersect_with_conjunction(self, conjunction, path):
        # Other operand is a disjunction -- look for path we can merge with,
        # if not found, append to conjunction.
        for cpath in conjunction.paths:
            if isinstance(cpath, (caos_ast.EntityLink, caos_ast.EntitySet)):
                merge = self.intersect_sets(cpath, path)
                if merge is cpath:
                    break
        else:
            conjunction = caos_ast.Conjunction(paths=frozenset(conjunction.paths | {path}))

        return conjunction

    def intersect_conjunctions(self, left, right, merge_filters=False):
        result = caos_ast.Conjunction(paths=left.paths)
        result.update(right)

        if len(result.paths) > 1:
            self.flatten_path_combination(result)
            self.unify_paths(result.paths, mode=result.__class__, reverse=False,
                             merge_filters=merge_filters)
            result.paths = frozenset(p for p in result.paths)

        return result

    def intersect_disjunctions(self, left, right):
        """Produce a conjunction of two disjunctions"""

        if left.paths and right.paths:
            # (a | b) & (c | d) --> a & c | a & d | b & c | b & d
            # We unroll the expression since it is highly probable that
            # the resulting conjunctions will merge and we'll get a simpler
            # expression which is we further attempt to minimize using boolean
            # minimizer.
            #
            paths = set()

            for l in left.paths:
                for r in right.paths:
                    paths.add(self.intersect_paths(l, r))

            result = self.minimize_disjunction(paths)
            return result

        else:
            # Degenerate case
            if not left.paths:
                paths = right.paths
            elif not right.paths:
                paths = left.paths

            if len(paths) <= 1:
                return caos_ast.Conjunction(paths=frozenset(paths))
            else:
                return caos_ast.Disjunction(paths=frozenset(paths))

    def intersect_disjunction_with_conjunction(self, disjunction, conjunction):
        if disjunction.paths and conjunction.paths:
            return caos_ast.Disjunction(paths=frozenset({disjunction, conjunction}))
        elif conjunction.paths:
            return conjunction
        elif disjunction.paths:
            return caos_ast.Conjunction(paths=frozenset({disjunction}))
        else:
            return caos_ast.Conjunction()

    def intersect_paths(self, left, right, merge_filters=False):
        if isinstance(left, (caos_ast.EntityLink, caos_ast.EntitySet)):
            if isinstance(right, (caos_ast.EntityLink, caos_ast.EntitySet)):
                # Both operands are sets -- simply merge them
                result = self.intersect_sets(left, right, merge_filters)

            elif isinstance(right, caos_ast.Disjunction):
                result = self.intersect_with_disjunction(right, left)

            elif isinstance(right, caos_ast.Conjunction):
                result = self.intersect_with_conjunction(right, left)

        elif isinstance(left, caos_ast.Disjunction):
            if isinstance(right, (caos_ast.EntityLink, caos_ast.EntitySet)):
                result = self.intersect_with_disjunction(left, right)

            elif isinstance(right, caos_ast.Disjunction):
                result = self.intersect_disjunctions(left, right)

            elif isinstance(right, caos_ast.Conjunction):
                result = self.intersect_disjunction_with_conjunction(left, right)

        elif isinstance(left, caos_ast.Conjunction):
            if isinstance(right, (caos_ast.EntityLink, caos_ast.EntitySet)):
                result = self.intersect_with_conjunction(left, right)

            elif isinstance(right, caos_ast.Disjunction):
                result = self.intersect_disjunction_with_conjunction(right, left)

            elif isinstance(right, caos_ast.Conjunction):
                result = self.intersect_conjunctions(left, right, merge_filters)

        return result

    @debug.debug
    def match_prefixes(self, our, other, ignore_filters):
        result = None

        if isinstance(our, caos_ast.EntityLink):
            link = our
            our_node = our.target
            if our_node is None:
                our_id = caos_utils.LinearPath(our.source.id)
                our_id.add(link.filter.labels, link.filter.direction, None)
                our_node = our.source
            else:
                our_id = our_node.id
        else:
            link = None
            our_node = our
            our_id = our.id

        if isinstance(other, caos_ast.EntityLink):
            other_link = other
            other_node = other.target
            if other_node is None:
                other_node = other.source
                other_id = caos_utils.LinearPath(other.source.id)
                other_id.add(other_link.filter.labels, other_link.filter.direction, None)
            else:
                other_id = other_node.id
        else:
            other_link = None
            other_node = other
            other_id = other.id

        if our_id[-1] is None and other_id[-1] is not None:
            other_id = caos_utils.LinearPath(other_id)
            other_id[-1] = None

        if other_id[-1] is None and our_id[-1] is not None:
            our_id = caos_utils.LinearPath(our_id)
            our_id[-1] = None


        """LOG [caos.graph.merge] MATCH PREFIXES
        print(' ' * self.nest, our, other, ignore_filters)
        print(' ' * self.nest, '   PATHS: ', our_id)
        print(' ' * self.nest, '      *** ', other_id)
        print(' ' * self.nest, '       EQ ', our_id == other_id)
        """

        ok = ((our_node is None and other_node is None) or
              (our_node is not None and other_node is not None and
                (our_id == other_id
                 and our_node.anchor == other_node.anchor
                 and (ignore_filters or (not our_node.filter and not other_node.filter
                                         and not our_node.conjunction.paths
                                         and our_node.conceptfilter == other_node.conceptfilter
                                         and not other_node.conjunction.paths))))
              and (not link or (link.filter == other_link.filter)))

        if ok:
            if other_link:
                result = other_link
            else:
                result = other_node

        """LOG [caos.graph.merge] MATCH PREFIXES RESULT
        print(' ' * self.nest, '    ----> ', result)
        """

        return result

    def fixup_refs(self, refs, newref):
        caos_ast.Base.fixup_refs(refs, newref)

    def extract_paths(self, path, reverse=False, resolve_arefs=True, recurse_subqueries=False):
        if isinstance(path, caos_ast.GraphExpr):
            if not recurse_subqueries:
                return None
            else:
                paths = set()

                if recurse_subqueries == 'once':
                    recurse_subqueries = False

                if path.generator:
                    normalized = self.extract_paths(path.generator, reverse, resolve_arefs,
                                                    recurse_subqueries)
                    if normalized:
                        paths.add(normalized)

                for part in ('selector', 'grouper', 'sorter'):
                    e = getattr(path, part)
                    if e:
                        for p in e:
                            normalized = self.extract_paths(p, reverse, resolve_arefs,
                                                            recurse_subqueries)
                            if normalized:
                                paths.add(normalized)

                if len(paths) == 1:
                    return next(iter(paths))
                else:
                    result = caos_ast.Disjunction(paths=frozenset(paths))
                    return self.flatten_path_combination(result)

        elif isinstance(path, caos_ast.SelectorExpr):
            return self.extract_paths(path.expr, reverse, resolve_arefs, recurse_subqueries)

        elif isinstance(path, caos_ast.SortExpr):
            return self.extract_paths(path.expr, reverse, resolve_arefs, recurse_subqueries)

        elif isinstance(path, (caos_ast.EntitySet, caos_ast.InlineFilter, caos_ast.AtomicRef)):
            if isinstance(path, (caos_ast.InlineFilter, caos_ast.AtomicRef)) and \
                                                    (resolve_arefs or reverse):
                result = path.ref
            else:
                result = path

            if isinstance(result, caos_ast.EntitySet):
                if reverse:
                    while result.rlink:
                        result = result.rlink.source
            return result

        elif isinstance(path, caos_ast.InlinePropFilter):
            return self.extract_paths(path.ref, reverse, resolve_arefs, recurse_subqueries)

        elif isinstance(path, caos_ast.LinkPropRef):
            if resolve_arefs or reverse:
                return self.extract_paths(path.ref, reverse, resolve_arefs, recurse_subqueries)
            else:
                return path

        elif isinstance(path, caos_ast.EntityLink):
            if reverse:
                result = path
                if path.source:
                    result = path.source
                    while result.rlink:
                        result = result.rlink.source
            else:
                result = path
            return result

        elif isinstance(path, caos_ast.PathCombination):
            result = set()
            for p in path.paths:
                normalized = self.extract_paths(p, reverse, resolve_arefs, recurse_subqueries)
                if normalized:
                    result.add(normalized)
            if len(result) == 1:
                return next(iter(result))
            else:
                return self.flatten_path_combination(path.__class__(paths=frozenset(result)))

        elif isinstance(path, caos_ast.BinOp):
            combination = caos_ast.Disjunction if self.is_weak_op(path.op) else caos_ast.Conjunction

            paths = set()
            for p in (path.left, path.right):
                normalized = self.extract_paths(p, reverse, resolve_arefs, recurse_subqueries)
                if normalized:
                    paths.add(normalized)

            if len(paths) == 1:
                return next(iter(paths))
            else:
                return self.flatten_path_combination(combination(paths=frozenset(paths)))

        elif isinstance(path, caos_ast.UnaryOp):
            return self.extract_paths(path.expr, reverse, resolve_arefs, recurse_subqueries)

        elif isinstance(path, caos_ast.TypeCast):
            return self.extract_paths(path.expr, reverse, resolve_arefs, recurse_subqueries)

        elif isinstance(path, caos_ast.NoneTest):
            return self.extract_paths(path.expr, reverse, resolve_arefs, recurse_subqueries)

        elif isinstance(path, caos_ast.FunctionCall):
            paths = set()
            for p in path.args:
                p = self.extract_paths(p, reverse, resolve_arefs, recurse_subqueries)
                if p:
                    paths.add(p)

            if len(paths) == 1:
                return next(iter(paths))
            else:
                return caos_ast.Conjunction(paths=frozenset(paths))

        elif isinstance(path, (caos_ast.Sequence, caos_ast.Record)):
            paths = set()
            for p in path.elements:
                p = self.extract_paths(p, reverse, resolve_arefs, recurse_subqueries)
                if p:
                    paths.add(p)

            if len(paths) == 1:
                return next(iter(paths))
            else:
                return caos_ast.Disjunction(paths=frozenset(paths))

        elif isinstance(path, caos_ast.Constant):
            return None

        elif isinstance(path, caos_ast.GraphExpr):
            return None

        else:
            assert False, 'unexpected node "%r"' % path

    def copy_path(self, path: (caos_ast.EntitySet, caos_ast.EntityLink)):
        if isinstance(path, caos_ast.EntitySet):
            result = caos_ast.EntitySet(id=path.id, anchor=path.anchor, concept=path.concept,
                                        users=path.users, joins=path.joins)
            rlink = path.rlink
        else:
            result = None
            rlink = path

        current = result

        while rlink:
            link = caos_ast.EntityLink(filter=rlink.filter, target=current,
                                       link_proto=rlink.link_proto,
                                       propfilter=rlink.propfilter,
                                       users=rlink.users.copy(),
                                       anchor=rlink.anchor)

            if not result:
                result = link

            parent_path = rlink.source

            if parent_path:
                parent = caos_ast.EntitySet(id=parent_path.id, anchor=parent_path.anchor,
                                            concept=parent_path.concept, users=parent_path.users,
                                            joins=parent_path.joins)
                parent.disjunction = caos_ast.Disjunction(paths=frozenset((link,)))
                link.source = parent

                if current:
                    current.rlink = link
                current = parent
                rlink = parent_path.rlink

            else:
                rlink = None

        return result

    def process_function_call(self, node):
        if node.name in (('search', 'rank'), ('search', 'headline')):
            refs = set()
            for arg in node.args:
                if isinstance(arg, caos_ast.EntitySet):
                    refs.add(arg)
                else:
                    refs.update(ast.find_children(arg, lambda n: isinstance(n, caos_ast.EntitySet),
                                                  force_traversal=True))

            assert len(refs) == 1

            ref = next(iter(refs))

            cols = []
            for link_name, link in ref.concept.get_searchable_links():
                id = LinearPath(ref.id)
                id.add(frozenset((link.first,)), caos_types.OutboundDirection, link.first.target)
                cols.append(caos_ast.AtomicRefSimple(ref=ref, name=link_name,
                                                     caoslink=link.first,
                                                     id=id))

            if not cols:
                raise caos_error.CaosError('%s call on concept %s without any search configuration'\
                                           % (node.name, ref.concept.name),
                                           hint='Configure search for "%s"' % ref.concept.name)

            ref.atomrefs.update(cols)

            node = caos_ast.FunctionCall(name=node.name,
                                         args=[caos_ast.Sequence(elements=cols), node.args[1]])

        elif node.name[0] == 'agg':
            node.aggregates = True

        if node.args:
            for arg in node.args:
                if not isinstance(arg, caos_ast.Constant):
                    break
            else:
                node = caos_ast.Constant(expr=node, type=node.args[0].type)

        return node

    def process_sequence(self, seq):
        pathdict = {}
        proppathdict = {}
        elems = []

        const = True

        for elem in seq.elements:
            if isinstance(elem, (caos_ast.BaseRef, caos_ast.Disjunction)):
                if not isinstance(elem, caos_ast.Disjunction):
                    elem = caos_ast.Disjunction(paths=frozenset({elem}))
                elif len(elem.paths) > 1:
                    break

                pd = self.check_atomic_disjunction(elem, caos_ast.AtomicRef)
                if not pd:
                    pd = self.check_atomic_disjunction(elem, caos_ast.LinkPropRef)
                    if not pd:
                        break
                    proppathdict.update(pd)
                else:
                    pathdict.update(pd)

                if pathdict and proppathdict:
                    break

                elems.append(next(iter(elem.paths)))
                const = False
            elif const and isinstance(elem, caos_ast.Constant):
                continue
            else:
                # The sequence is not all atoms
                break
        else:
            if const:
                return caos_ast.Constant(expr=seq)
            else:
                if len(pathdict) == 1:
                    exprtype = caos_ast.AtomicRefExpr
                elif len(proppathdict) == 1:
                    exprtype = caos_ast.LinkPropRefExpr
                    pathdict = proppathdict
                else:
                    exprtype = None

                if exprtype:
                    # The sequence is composed of references to atoms of the same node
                    ref = list(pathdict.values())[0]

                    for elem in elems:
                        if elem.ref is not ref.ref:
                            elem.replace_refs([elem.ref], ref.ref, deep=True)

                    return exprtype(expr=caos_ast.Sequence(elements=elems))

        return seq

    def check_atomic_disjunction(self, expr, typ):
        """Check that all paths in disjunction are atom references.

           Return a dict mapping path prefixes to a corresponding node.
        """
        pathdict = {}
        for ref in expr.paths:
            # Check that refs in the operand are all atomic: non-atoms do not coerce
            # to literals.
            #
            if not isinstance(ref, typ):
                return None

            if isinstance(ref, caos_ast.AtomicRef):
                ref_id = ref.ref.id
            else:
                ref_id = ref.id

            #assert not pathdict.get(ref_id)
            pathdict[ref_id] = ref
        return pathdict

    def process_binop(self, left, right, op):
        try:
            result = self._process_binop(left, right, op, reversed=False)
        except TreeError:
            result = self._process_binop(right, left, op, reversed=True)

        return result

    def is_join(self, left, right, op, reversed):
        return isinstance(left, caos_ast.Path) and isinstance(right, caos_ast.Path) and \
               op in (ast.ops.EQ, ast.ops.NE)

    def is_type_check(self, left, right, op, reversed):
        return not reversed and op in (ast.ops.IS, ast.ops.IS_NOT) and \
                isinstance(left, caos_ast.Path) and isinstance(right, caos_types.ProtoConcept)

    def is_const_idfilter(self, left, right, op, reversed):
        return isinstance(left, caos_ast.Path) and isinstance(right, caos_ast.Constant) and \
                (op in (ast.ops.IN, ast.ops.NOT_IN) or \
                 (not reversed and op in (ast.ops.EQ, ast.ops.NE)))

    def get_multipath(self, expr:caos_ast.Path):
        if not isinstance(expr, caos_ast.PathCombination):
            expr = caos_ast.Disjunction(paths=frozenset((expr,)))
        return expr

    def path_from_set(self, paths):
        if len(paths) == 1:
            return next(iter(paths))
        else:
            return caos_ast.Disjunction(paths=frozenset(paths))

    def _process_binop(self, left, right, op, reversed=False):
        result = None

        def newbinop(left, right, operation=None):
            operation = operation or op
            if reversed:
                return caos_ast.BinOp(left=right, op=operation, right=left)
            else:
                return caos_ast.BinOp(left=left, op=operation, right=right)

        left_paths = self.extract_paths(left, reverse=False, resolve_arefs=False)

        if isinstance(left_paths, caos_ast.Path):
            # If both left and right operands are references to atoms of the same node,
            # or one of the operands is a reference to an atom and other is a constant,
            # then fold the expression into an in-line filter of that node.
            #

            left_exprs = self.get_multipath(left_paths)

            pathdict = self.check_atomic_disjunction(left_exprs, caos_ast.AtomicRef)
            proppathdict = self.check_atomic_disjunction(left_exprs, caos_ast.LinkPropRef)

            is_agg = self.is_aggregated_expr(left, deep=True) or \
                     self.is_aggregated_expr(right, deep=True)

            if is_agg:
                result = newbinop(left, right)

            elif not pathdict and not proppathdict:

                if self.is_join(left, right, op, reversed):
                    # Concept join expression: <path> {==|!=} <path>

                    right_exprs = self.get_multipath(right)

                    id_col = caos_name.Name('semantix.caos.builtins.id')
                    lrefs = [caos_ast.AtomicRefSimple(ref=p, name=id_col)
                                for p in left_exprs.paths]
                    rrefs = [caos_ast.AtomicRefSimple(ref=p, name=id_col)
                                for p in right_exprs.paths]

                    l = caos_ast.Disjunction(paths=frozenset(lrefs))
                    r = caos_ast.Disjunction(paths=frozenset(rrefs))
                    result = newbinop(l, r)

                    for lset, rset in itertools.product(left_exprs.paths, right_exprs.paths):
                        lset.joins.add(rset)
                        rset.backrefs.add(lset)
                        rset.joins.add(lset)
                        lset.backrefs.add(rset)

                elif self.is_type_check(left, right, op, reversed):
                    # Type check expression: <path> IS [NOT] <concept>

                    paths = set()

                    for path in left_exprs.paths:
                        if op == ast.ops.IS:
                            if path.concept.issubclass(self.context.current.proto_schema, right):
                                paths.add(path)
                        elif op == ast.ops.IS_NOT:
                            if path.concept != right:
                                filtered = path.concept.filter_children(lambda i: i != right)
                                if filtered[path.concept]:
                                    path.conceptfilter = filtered
                                paths.add(path)

                    result = self.path_from_set(paths)

                elif self.is_const_idfilter(left, right, op, reversed):
                    # Constant id filter expressions:
                    #       <path> IN <const_id_list>
                    #       <const_id> IN <path>
                    #       <path> = <const_id>

                    id_col = caos_name.Name('semantix.caos.builtins.id')

                    # <Constant> IN <EntitySet> is interpreted as a membership
                    # check of entity with ID represented by Constant in the EntitySet,
                    # which is equivalent to <EntitySet>.id = <Constant>
                    #
                    if reversed:
                        membership_op = ast.ops.EQ if op == ast.ops.IN else ast.ops.NE
                    else:
                        membership_op = op

                    paths = set()
                    for p in left_exprs.paths:
                        ref = caos_ast.AtomicRefSimple(ref=p, name=id_col)
                        expr = caos_ast.BinOp(left=ref, right=right, op=membership_op)
                        paths.add(caos_ast.AtomicRefExpr(expr=expr))

                    result = self.path_from_set(paths)

                elif op == caos_ast.SEARCH:
                    paths = set()
                    for p in left_exprs.paths:
                        searchable = list(p.concept.get_searchable_links())
                        if not searchable:
                            err = '%s operator called on concept %s without any search configuration'\
                                                       % (caos_ast.SEARCH, p.concept.name)
                            hint = 'Configure search for "%s"' % p.concept.name
                            raise caos_error.CaosError(err, hint=hint)

                        # A SEARCH operation on an entity set is always an inline filter ATM
                        paths.add(caos_ast.AtomicRefExpr(expr=newbinop(p, right)))

                    result = self.path_from_set(paths)

                if not result:
                    result = newbinop(left, right)
            else:
                right_paths = self.extract_paths(right, reverse=False, resolve_arefs=False)

                if isinstance(right, caos_ast.Constant):
                    paths = set()

                    if proppathdict:
                        exprnode_type = caos_ast.LinkPropRefExpr
                        refdict = proppathdict
                    else:
                        exprnode_type = caos_ast.AtomicRefExpr
                        refdict = pathdict

                    if isinstance(left, caos_ast.Path):
                        # We can only break up paths, and must not pick paths out of other
                        # expressions
                        #
                        for ref in left_exprs.paths:
                            if isinstance(ref, exprnode_type) \
                                                and isinstance(op, ast.ops.BooleanOperator):
                                # We must not inline boolean expressions beyond the original bin-op
                                result = newbinop(left, right)
                                break
                            paths.add(exprnode_type(expr=newbinop(ref, right)))
                        else:
                            result = self.path_from_set(paths)

                    elif len(refdict) == 1:
                        # Left operand references a single entity
                        result = exprnode_type(expr=newbinop(left, right))
                    else:
                        result = newbinop(left, right)

                elif isinstance(right_paths, caos_ast.Path):
                    right_exprs = self.get_multipath(right_paths)

                    rightdict = self.check_atomic_disjunction(right_exprs, caos_ast.AtomicRef)
                    rightpropdict = self.check_atomic_disjunction(right_exprs, caos_ast.LinkPropRef)

                    if rightdict and pathdict or rightpropdict and proppathdict:
                        paths = set()

                        if proppathdict:
                            exprtype = caos_ast.LinkPropRefExpr
                            leftdict = proppathdict
                            rightdict = rightpropdict
                        else:
                            exprtype = caos_ast.AtomicRefExpr
                            leftdict = pathdict


                        # If both operands are atom references, then we check if the referenced
                        # atom parent concepts intersect, and if they do we fold the expression
                        # into the atom ref for those common concepts only.  If there are no common
                        # concepts, a regular binary operation is returned.
                        #
                        if isinstance(left, caos_ast.Path) and isinstance(right, caos_ast.Path):
                            # We can only break up paths, and must not pick paths out of other
                            # expressions
                            #

                            for ref in left_exprs.paths:
                                if isinstance(ref, caos_ast.AtomicRef):
                                    left_id = ref.ref.id
                                else:
                                    left_id = ref.id

                                right_expr = rightdict.get(left_id)

                                if right_expr:
                                    right_expr.replace_refs([right_expr.ref], ref.ref, deep=True)
                                    filterop = newbinop(ref, right_expr)
                                    paths.add(exprtype(expr=filterop))

                            if paths:
                                result = self.path_from_set(paths)
                            else:
                                result = newbinop(left, right)

                        elif len(rightdict) == 1 and len(leftdict) == 1 and \
                                next(iter(leftdict)) == next(iter(rightdict)):

                            newref = next(iter(leftdict.values()))
                            refs = [p.ref for p in right_exprs.paths]
                            right.replace_refs(refs, newref.ref, deep=True)
                            # Left and right operand reference the same single path
                            result = exprtype(expr=newbinop(left, right))

                        else:
                            result = newbinop(left, right)
                    else:
                        result = newbinop(left, right)

                elif isinstance(right, caos_ast.BinOp) and op == right.op and \
                                                           isinstance(left, caos_ast.Path):
                    # Got a bin-op, that was not folded into an atom ref.  Re-check it since
                    # we may use operator associativity to fold one of the operands
                    #
                    assert not proppathdict

                    folded_operand = None
                    for operand in (right.left, right.right):
                        if isinstance(operand, caos_ast.AtomicRef):
                            operand_id = operand.ref.id
                            ref = pathdict.get(operand_id)
                            if ref:
                                ref.expr = self.extend_binop(ref.expr, operand, op=op,
                                                                                reverse=reversed)
                                folded_operand = operand
                                break

                    if folded_operand:
                        other_operand = right.left if folded_operand is right.right else right.right
                        result = newbinop(left, other_operand)
                    else:
                        result = newbinop(left, right)

        elif isinstance(left, caos_ast.Constant):
            if isinstance(right, caos_ast.Constant):
                l, r = (right, left) if reversed else (left, right)
                if l.type == r.type:
                    result_type = l.type
                else:
                    schema = self.context.current.proto_schema
                    result_type = caos_types.TypeRules.get_result(op, (l.type, r.type), schema)
                result = caos_ast.Constant(expr=newbinop(left, right), type=result_type)

        elif isinstance(left, caos_ast.BinOp):
            result = newbinop(left, right)

        elif isinstance(left, caos_ast.TypeCast):
            result = newbinop(left, right)

        elif isinstance(left, caos_ast.FunctionCall):
            result = newbinop(left, right)

        if not result:
            raise TreeError('unexpected binop operands: %s, %s' % (left, right))

        return result

    def process_unaryop(self, expr, operator):
        if isinstance(expr, caos_ast.AtomicRef):
            result = caos_ast.AtomicRefExpr(expr=caos_ast.UnaryOp(expr=expr, op=operator))
        elif isinstance(expr, caos_ast.LinkPropRef):
            result = caos_ast.LinkPropRefExpr(expr=caos_ast.UnaryOp(expr=expr, op=operator))
        else:
            paths = self.extract_paths(expr, reverse=False, resolve_arefs=False)
            exprs = self.get_multipath(paths)
            arefs = self.check_atomic_disjunction(exprs, caos_ast.AtomicRef)
            proprefs = self.check_atomic_disjunction(exprs, caos_ast.LinkPropRef)

            if arefs and len(arefs) == 1:
                result = caos_ast.AtomicRefExpr(expr=caos_ast.UnaryOp(expr=expr, op=operator))
            elif proprefs and len(proprefs) == 1:
                result = caos_ast.LinkPropRefExpr(expr=caos_ast.UnaryOp(expr=expr, op=operator))
            else:
                result = caos_ast.UnaryOp(expr=expr, op=operator)

        return result

    def process_none_test(self, expr):
        if isinstance(expr.expr, caos_ast.AtomicRef):
            expr = caos_ast.AtomicRefExpr(expr=expr)
        elif isinstance(expr.expr, caos_ast.LinkPropRef):
            expr = caos_ast.LinkPropRefExpr(expr=expr)

        return expr

    def eval_const_bool_expr(self, left, right, op, reversed):
        if op == 'and':
            if not left.value:
                return caos_ast.Constant(value=False)
            else:
                return right
        elif op == 'or':
            if left.value:
                return caos_ast.Constant(value=True)
            else:
                return right

    def eval_const_expr(self, left, right, op, reversed):
        if isinstance(op, ast.ops.BooleanOperator):
            return self.eval_const_bool_expr(left, right, op, reversed)
        elif op == '=':
            op = '=='

        if reversed:
            params = (right.value, op, left.value)
        else:
            params = (left.value, op, right.value)

        return caos_ast.Constant(value=eval('%r %s %r' % params))

    def get_expr_type(self, expr, schema):
        if isinstance(expr, caos_ast.MetaRef):
            result = str
        elif isinstance(expr, caos_ast.AtomicRefSimple):
            if isinstance(expr.ref, caos_ast.PathCombination):
                targets = [t.concept for t in expr.ref.paths]
                concept = caos_utils.get_prototype_nearest_common_ancestor(targets, schema)
            else:
                concept = expr.ref.concept

            if expr.name == 'semantix.caos.builtins.id':
                result = concept
            else:
                linkset = concept.get_attr(schema, expr.name)
                assert linkset, '"%s" is not a link of "%s"' % (expr.name, concept.name)
                targets = [l.target for l in linkset]

                if len(targets) == 1:
                    result = targets[0]
                else:
                    result = caos_utils.get_prototype_nearest_common_ancestor(targets, schema)

        elif isinstance(expr, caos_ast.LinkPropRefSimple):
            if isinstance(expr.ref, caos_ast.PathCombination):
                targets = [t.link_proto for t in expr.ref.paths]
                link = caos_utils.get_prototype_nearest_common_ancestor(targets, schema)
            else:
                link = expr.ref.link_proto

            prop = link.get_attr(schema, expr.name)
            assert prop, '"%s" is not a property of "%s"' % (expr.name, link.name)
            result = prop.target

        elif isinstance(expr, caos_ast.BaseRefExpr):
            result = self.get_expr_type(expr.expr, schema)

        elif isinstance(expr, caos_ast.Record):
            result = expr.concept

        elif isinstance(expr, caos_ast.FunctionCall):
            argtypes = tuple(self.get_expr_type(arg, schema) for arg in expr.args)
            result = caos_types.TypeRules.get_result(expr.name, argtypes, schema)

            if result is None:
                fcls = caos_types.FunctionMeta.get_function_class(expr.name)
                if fcls:
                    signature = fcls.get_signature(argtypes)
                    if signature and signature[2]:
                        result = schema.get(signature[2])

        elif isinstance(expr, caos_ast.Constant):
            #assert expr.type is not None or expr.value is not None

            if expr.type:
                result = expr.type
            elif expr.value is not None:
                result = expr.value.__class__
            else:
                result = None

        elif isinstance(expr, caos_ast.BinOp):
            left_type = self.get_expr_type(expr.left, schema)
            right_type = self.get_expr_type(expr.right, schema)
            result = caos_types.TypeRules.get_result(expr.op, (left_type, right_type), schema)

        elif isinstance(expr, caos_ast.Disjunction):
            if expr.paths:
                result = self.get_expr_type(next(iter(expr.paths)), schema)
            else:
                result = None
        else:
            result = None

        return result

    def get_selector_types(self, selector, schema):
        result = collections.OrderedDict()

        for i, selexpr in enumerate(selector):
            result[selexpr.name or str(i)] = (self.get_expr_type(selexpr.expr, schema),
                                              isinstance(selexpr.expr, caos_ast.Constant))

        return result
