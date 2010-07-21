##
# Copyright (c) 2010 Sprymix Inc.
# All rights reserved.
#
# See LICENSE for details.
##


from semantix import caos
from semantix.caos import proto
from semantix.utils import ast
from . import ast as caosql_ast, parser, transformer, codegen
from semantix.caos.tree import ast as caos_ast

from . import errors


class CaosQLExpression:
    def __init__(self, proto_schema, module_aliases=None):
        self.parser = parser.CaosQLParser()
        self.module_aliases = module_aliases
        self.proto_schema = proto_schema
        self.transformer = transformer.CaosqlTreeTransformer(proto_schema, module_aliases)
        self.reverse_transformer = transformer.CaosqlReverseTransformer()

    def process_concept_expr(self, expr, concept):
        tree = self.parser.parse(expr)
        context = transformer.ParseContext()
        context.current.location = 'selector'
        return self.transformer._process_expr(context, tree)

    def normalize_refs(self, expr, module_aliases=None):
        tree = self.parser.parse(expr)
        tree = self.transformer.normalize_refs(tree, module_aliases=module_aliases)
        return codegen.CaosQLSourceGenerator.to_source(tree)

    def normalize_expr(self, expr, module_aliases=None, anchors=None):
        tree = self.parser.parse(expr)
        tree = self.parser.normalize_select_query(tree)
        caos_tree = self.transformer.transform(tree, (), module_aliases=module_aliases,
                                                         anchors=anchors)
        tree = self.reverse_transformer.transform(caos_tree)
        return codegen.CaosQLSourceGenerator.to_source(tree), caos_tree

    def normalize_source_expr(self, expr, source):
        tree = self.parser.parse(expr)

        visitor = _PrependSource(source, self.proto_schema, self.module_aliases)
        visitor.visit(tree)

        expr = codegen.CaosQLSourceGenerator.to_source(tree)
        return expr, tree

    def check_source_atomic_expr(self, tree, source):
        context = transformer.ParseContext()
        context.current.location = 'selector'
        processed = self.transformer._process_expr(context, tree)

        ok = isinstance(processed, caos_ast.BaseRef) \
             or (isinstance(processed, caos_ast.Disjunction) and
                 isinstance(list(processed.paths)[0], caos_ast.BaseRef))

        if not ok:
            msg = "invalid link reference"
            details = "Expression must only contain references to local atoms"
            raise errors.CaosQLReferenceError(msg, details=details)

        return processed

    def get_node_references(self, tree):
        refs = self.transformer.extract_paths(tree, reverse=True, resolve_arefs=True,
                                                    recurse_subqueries=True)

        flt = lambda n: isinstance(n, (caos_ast.EntitySet, caos_ast.EntityLink))
        nodes = ast.find_children(refs, flt)

        result = []
        if nodes:
            for node in nodes:
                if isinstance(node, caos_ast.EntitySet):
                    result.append(node.concept)
                else:
                    result.append(node.link_proto)

        return set(result)


class _PrependSource(ast.visitor.NodeVisitor):
    def __init__(self, source, schema, module_aliases):
        self.source = source
        self.schema = schema
        self.module_aliases = module_aliases

    def visit_PathNode(self, node):
        step = node.steps[0]

        if step.namespace:
            name = caos.Name(name=step.expr, module=step.namespace)
        else:
            name = step.expr

        if isinstance(self.source, caos.types.ProtoLink):
            type = proto.LinkProperty
        else:
            type = proto.Link

        prototype = self.schema.get(name, None)

        if not prototype:
            prototype = self.schema.get(name, type=type, module_aliases=self.module_aliases)

        if not isinstance(prototype, self.source.__class__.get_canonical_class()):

            pointer_node = caosql_ast.LinkNode(name=prototype.name.name,
                                               namespace=prototype.name.module)

            if isinstance(self.source, caos.types.ProtoLink):
                link = caosql_ast.LinkPropExprNode(expr=pointer_node)
            else:
                link = caosql_ast.LinkExprNode(expr=pointer_node)

            source = self.source.get_pointer_origin(self.schema, prototype.name, farthest=True)
            source = caosql_ast.PathStepNode(expr=source.name.name, namespace=source.name.module)
            node.steps[0] = source
            node.steps.insert(1, link)
            offset = 2
        else:
            offset = 0

        steps = []
        for step in node.steps[offset:]:
            steps.append(self.visit(step))
        node.steps[offset:] = steps
        return node

    def visit_PathStepNode(self, node):
        if node.namespace:
            name = caos.Name(name=node.expr, module=node.namespace)
        else:
            name = node.expr

        if isinstance(self.source, caos.types.ProtoLink):
            type = proto.LinkProperty
        else:
            type = proto.Link

        prototype = self.schema.get(name, type=type, module_aliases=self.module_aliases)

        node.expr = prototype.name.name
        node.namespace = prototype.name.module
        return node

    def visit_LinkExprNode(self, node):
        expr = self.visit(node.expr)

        if isinstance(self.source, caos.types.ProtoLink):
            node = caosql_ast.LinkPropExprNode(expr=expr)

        return node

    def visit_LinkNode(self, node):
        if node.namespace:
            name = caos.Name(name=node.name, module=node.namespace)
        else:
            name = node.expr

        prototype = self.schema.get(name, module_aliases=self.module_aliases)

        node.name = prototype.name.name
        node.namespace = prototype.name.module
        return node
