import ast


class UnsupportedSyntaxError(ValueError):
    pass


class CFGInstrumenter:
    """Build a deterministic source-level CFG while inserting runtime hooks."""

    def __init__(self, source):
        self.source = source
        self.next_block = 1
        self.blocks = {}
        self.edges = []

    def new_block(self, kind, statements):
        block_id = f"B{self.next_block:03d}"
        self.next_block += 1
        first = statements[0]
        last = statements[-1]
        if kind == "if_predicate":
            block_source = f"if {ast.unparse(first.test)}"
        elif kind == "while_header":
            block_source = f"while {ast.unparse(first.test)}"
        elif kind == "for_header":
            block_source = f"for {ast.unparse(first.target)} in {ast.unparse(first.iter)}"
        else:
            block_source = "\n".join(
                filter(None, (ast.get_source_segment(self.source, statement) for statement in statements))
            )
        self.blocks[block_id] = {
            "kind": kind,
            "source_span": [
                getattr(first, "lineno", None),
                getattr(first, "col_offset", None),
                getattr(last, "end_lineno", getattr(last, "lineno", None)),
                getattr(last, "end_col_offset", None),
            ],
            "source": block_source,
        }
        return block_id

    def add_edge(self, source, target, edge_type):
        edge = {"from": source, "to": target, "edge_type": edge_type}
        if edge not in self.edges:
            self.edges.append(edge)

    def connect(self, exits, target):
        for source, edge_type in exits:
            self.add_edge(source, target, edge_type)

    @staticmethod
    def locals_call():
        return ast.Call(ast.Name("locals", ast.Load()), [], [])

    @classmethod
    def runtime_expr(cls, method, block_id, extra_args=None):
        return ast.Expr(ast.Call(
            ast.Attribute(ast.Name("__trace__", ast.Load()), method, ast.Load()),
            [ast.Constant(block_id), *(extra_args or []), cls.locals_call()],
            [],
        ))

    @classmethod
    def predicate_call(cls, block_id, test, predicate_kind):
        return ast.Call(
            ast.Attribute(ast.Name("__trace__", ast.Load()), "predicate", ast.Load()),
            [ast.Constant(block_id), test, cls.locals_call(), ast.Constant(predicate_kind)],
            [],
        )

    @staticmethod
    def contains_loop_jump(statements):
        for statement in statements:
            for node in ast.walk(statement):
                if isinstance(node, (ast.Break, ast.Continue)):
                    return True
        return False

    def instrument_sequence(self, statements):
        output = []
        entry = None
        pending = []
        index = 0
        while index < len(statements):
            statement = statements[index]

            if isinstance(statement, ast.If):
                block_id = self.new_block("if_predicate", [statement])
                self.connect(pending, block_id)
                entry = entry or block_id
                body, body_entry, body_exits = self.instrument_sequence(statement.body)
                orelse, else_entry, else_exits = self.instrument_sequence(statement.orelse)
                statement.test = self.predicate_call(block_id, statement.test, "if")
                statement.body = body
                statement.orelse = orelse
                if body_entry:
                    self.add_edge(block_id, body_entry, "branch_true")
                else:
                    body_exits = [(block_id, "branch_true")]
                if else_entry:
                    self.add_edge(block_id, else_entry, "branch_false")
                else:
                    else_exits = [(block_id, "branch_false")]
                pending = body_exits + else_exits
                output.append(statement)
                index += 1
                continue

            if isinstance(statement, ast.While):
                if statement.orelse or self.contains_loop_jump(statement.body):
                    raise UnsupportedSyntaxError("while-else and break/continue are not supported in the local MVP")
                block_id = self.new_block("while_header", [statement])
                self.connect(pending, block_id)
                entry = entry or block_id
                body, body_entry, body_exits = self.instrument_sequence(statement.body)
                statement.test = self.predicate_call(block_id, statement.test, "while")
                statement.body = body
                if body_entry:
                    self.add_edge(block_id, body_entry, "loop_body")
                for body_exit, _ in body_exits:
                    self.add_edge(body_exit, block_id, "backedge")
                pending = [(block_id, "loop_exit")]
                output.append(statement)
                index += 1
                continue

            if isinstance(statement, (ast.For, ast.AsyncFor)):
                if statement.orelse or self.contains_loop_jump(statement.body):
                    raise UnsupportedSyntaxError("for-else and break/continue are not supported in the local MVP")
                block_id = self.new_block("for_header", [statement])
                self.connect(pending, block_id)
                entry = entry or block_id
                body, body_entry, body_exits = self.instrument_sequence(statement.body)
                iteration_hook = ast.copy_location(
                    self.runtime_expr("for_iteration", block_id),
                    statement.body[0],
                )
                statement.body = [iteration_hook, *body]
                if body_entry:
                    self.add_edge(block_id, body_entry, "loop_body")
                for body_exit, _ in body_exits:
                    self.add_edge(body_exit, block_id, "backedge")
                exit_hook = ast.copy_location(self.runtime_expr("for_exit", block_id), statement)
                output.extend([statement, exit_hook])
                pending = [(block_id, "loop_exit")]
                index += 1
                continue

            if isinstance(statement, ast.Return):
                block_id = self.new_block("return", [statement])
                self.connect(pending, block_id)
                entry = entry or block_id
                value = statement.value if statement.value is not None else ast.Constant(None)
                statement.value = ast.Call(
                    ast.Attribute(ast.Name("__trace__", ast.Load()), "return_value", ast.Load()),
                    [ast.Constant(block_id), value, self.locals_call()],
                    [],
                )
                self.add_edge(block_id, None, "return")
                output.append(statement)
                pending = []
                index += 1
                continue

            if isinstance(statement, (ast.Try, ast.With, ast.AsyncWith, ast.Raise)) or (
                hasattr(ast, "Match") and isinstance(statement, ast.Match)
            ):
                raise UnsupportedSyntaxError(f"{type(statement).__name__} is not supported in the local MVP")

            group = []
            while index < len(statements):
                candidate = statements[index]
                if isinstance(candidate, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.Return, ast.Try, ast.With, ast.AsyncWith, ast.Raise)) or (
                    hasattr(ast, "Match") and isinstance(candidate, ast.Match)
                ):
                    break
                group.append(candidate)
                index += 1
            if not group:
                raise UnsupportedSyntaxError(f"cannot instrument {type(statement).__name__}")
            block_id = self.new_block("sequential", group)
            self.connect(pending, block_id)
            entry = entry or block_id
            output.extend([
                ast.copy_location(self.runtime_expr("enter", block_id), group[0]),
                *group,
                ast.copy_location(self.runtime_expr("exit", block_id), group[-1]),
            ])
            pending = [(block_id, "fallthrough")]
        return output, entry, pending

    def instrument_function(self, tree, function_name):
        target = next(
            (
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
            ),
            None,
        )
        if target is None:
            raise ValueError(f"function not found: {function_name}")
        target.body, entry, exits = self.instrument_sequence(target.body)
        self.connect(exits, None)
        ast.fix_missing_locations(tree)
        return {
            "entry_block": entry,
            "blocks": self.blocks,
            "edges": self.edges,
            "instrumented_tree": tree,
        }


def build_cfg_and_instrument(source, function_name, filename="<source>"):
    tree = ast.parse(source, filename=filename)
    return CFGInstrumenter(source).instrument_function(tree, function_name)
