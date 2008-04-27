# -*- coding: utf-8 -*-
"""
    jinja2.parser
    ~~~~~~~~~~~~~

    Implements the template parser.

    :copyright: 2008 by Armin Ronacher.
    :license: BSD, see LICENSE for more details.
"""
from operator import itemgetter
from jinja2 import nodes
from jinja2.exceptions import TemplateSyntaxError


statement_end_tokens = set(['variable_end', 'block_end', 'in'])
_statement_keywords = frozenset(['for', 'if', 'block', 'extends', 'print',
                                 'macro', 'include', 'from', 'import'])
_compare_operators = frozenset(['eq', 'ne', 'lt', 'lteq', 'gt', 'gteq', 'in'])
_tuple_edge_tokens = set(['rparen']) | statement_end_tokens


class Parser(object):
    """The template parser class.

    Transforms sourcecode into an abstract syntax tree.
    """

    def __init__(self, environment, source, filename=None):
        self.environment = environment
        if isinstance(filename, unicode):
            filename = filename.encode('utf-8')
        self.source = unicode(source)
        self.filename = filename
        self.closed = False
        self.stream = environment.lexer.tokenize(source, filename)
        self.extensions = {}
        for extension in environment.extensions:
            for tag in extension.tags:
                self.extensions[tag] = extension.parse

    def parse_statement(self):
        """Parse a single statement."""
        token_type = self.stream.current.type
        if token_type in _statement_keywords:
            return getattr(self, 'parse_' + token_type)()
        elif token_type is 'call':
            return self.parse_call_block()
        elif token_type is 'filter':
            return self.parse_filter_block()
        elif token_type is 'name':
            ext = self.extensions.get(self.stream.current.value)
            if ext is not None:
                return ext(self)
        lineno = self.stream.current
        expr = self.parse_tuple()
        if self.stream.current.type == 'assign':
            result = self.parse_assign(expr)
        else:
            result = nodes.ExprStmt(expr, lineno=lineno)
        return result

    def parse_assign(self, target):
        """Parse an assign statement."""
        lineno = self.stream.expect('assign').lineno
        if not target.can_assign():
            raise TemplateSyntaxError("can't assign to '%s'" %
                                      target.__class__.__name__.lower(),
                                      target.lineno, self.filename)
        expr = self.parse_tuple()
        target.set_ctx('store')
        return nodes.Assign(target, expr, lineno=lineno)

    def parse_statements(self, end_tokens, drop_needle=False):
        """Parse multiple statements into a list until one of the end tokens
        is reached.  This is used to parse the body of statements as it also
        parses template data if appropriate.
        """
        # the first token may be a colon for python compatibility
        if self.stream.current.type is 'colon':
            self.stream.next()

        # in the future it would be possible to add whole code sections
        # by adding some sort of end of statement token and parsing those here.
        self.stream.expect('block_end')
        result = self.subparse(end_tokens)

        if drop_needle:
            self.stream.next()
        return result

    def parse_for(self):
        """Parse a for loop."""
        lineno = self.stream.expect('for').lineno
        target = self.parse_tuple(simplified=True)
        if not target.can_assign():
            raise TemplateSyntaxError("can't assign to '%s'" %
                                      target.__class__.__name__.lower(),
                                      target.lineno, self.filename)
        target.set_ctx('store')
        self.stream.expect('in')
        iter = self.parse_tuple(no_condexpr=True)
        test = None
        if self.stream.current.type is 'if':
            self.stream.next()
            test = self.parse_expression()
        body = self.parse_statements(('endfor', 'else'))
        if self.stream.next().type is 'endfor':
            else_ = []
        else:
            else_ = self.parse_statements(('endfor',), drop_needle=True)
        return nodes.For(target, iter, body, else_, test, lineno=lineno)

    def parse_if(self):
        """Parse an if construct."""
        node = result = nodes.If(lineno=self.stream.expect('if').lineno)
        while 1:
            # TODO: exclude conditional expressions here
            node.test = self.parse_tuple()
            node.body = self.parse_statements(('elif', 'else', 'endif'))
            token_type = self.stream.next().type
            if token_type is 'elif':
                new_node = nodes.If(lineno=self.stream.current.lineno)
                node.else_ = [new_node]
                node = new_node
                continue
            elif token_type is 'else':
                node.else_ = self.parse_statements(('endif',),
                                                   drop_needle=True)
            else:
                node.else_ = []
            break
        return result

    def parse_block(self):
        node = nodes.Block(lineno=self.stream.expect('block').lineno)
        node.name = self.stream.expect('name').value
        node.body = self.parse_statements(('endblock',), drop_needle=True)
        return node

    def parse_extends(self):
        node = nodes.Extends(lineno=self.stream.expect('extends').lineno)
        node.template = self.parse_expression()
        return node

    def parse_include(self):
        node = nodes.Include(lineno=self.stream.expect('include').lineno)
        node.template = self.parse_expression()
        return node

    def parse_import(self):
        node = nodes.Import(lineno=self.stream.expect('import').lineno)
        node.template = self.parse_expression()
        self.stream.expect('name:as')
        node.target = self.stream.expect('name').value
        if not nodes.Name(node.target, 'store').can_assign():
            raise TemplateSyntaxError('can\'t assign imported template '
                                      'to %r' % node.target, node.lineno,
                                      self.filename)
        return node

    def parse_from(self):
        node = nodes.FromImport(lineno=self.stream.expect('from').lineno)
        node.template = self.parse_expression()
        self.stream.expect('import')
        node.names = []
        while 1:
            if node.names:
                self.stream.expect('comma')
            if self.stream.current.type is 'name':
                target = nodes.Name(self.stream.current.value, 'store')
                if not target.can_assign():
                    raise TemplateSyntaxError('can\'t import object named %r'
                                              % target.name, target.lineno,
                                              self.filename)
                elif target.name.startswith('__'):
                    raise TemplateAssertionError('names starting with two '
                                                 'underscores can not be '
                                                 'imported', target.lineno,
                                                 self.filename)
                self.stream.next()
                if self.stream.current.test('name:as'):
                    self.stream.next()
                    alias = self.stream.expect('name')
                    if not nodes.Name(alias.value, 'store').can_assign():
                        raise TemplateSyntaxError('can\'t name imported '
                                                  'object %r.' % alias.value,
                                                  alias.lineno, self.filename)
                    node.names.append((target.name, alias.value))
                else:
                    node.names.append(target.name)
                if self.stream.current.type is not 'comma':
                    break
            else:
                break
        if self.stream.current.type is 'comma':
            self.stream.next()
        return node

    def parse_signature(self, node):
        node.args = args = []
        node.defaults = defaults = []
        self.stream.expect('lparen')
        while self.stream.current.type is not 'rparen':
            if args:
                self.stream.expect('comma')
            token = self.stream.expect('name')
            arg = nodes.Name(token.value, 'param', lineno=token.lineno)
            if not arg.can_assign():
                raise TemplateSyntaxError("can't assign to '%s'" %
                                          arg.name, arg.lineno,
                                          self.filename)
            if self.stream.current.type is 'assign':
                self.stream.next()
                defaults.append(self.parse_expression())
            args.append(arg)
        self.stream.expect('rparen')

    def parse_call_block(self):
        node = nodes.CallBlock(lineno=self.stream.expect('call').lineno)
        if self.stream.current.type is 'lparen':
            self.parse_signature(node)
        else:
            node.args = []
            node.defaults = []

        node.call = self.parse_expression()
        if not isinstance(node.call, nodes.Call):
            raise TemplateSyntaxError('expected call', node.lineno,
                                      self.filename)
        node.body = self.parse_statements(('endcall',), drop_needle=True)
        return node

    def parse_filter_block(self):
        node = nodes.FilterBlock(lineno=self.stream.expect('filter').lineno)
        node.filter = self.parse_filter(None, start_inline=True)
        node.body = self.parse_statements(('endfilter',), drop_needle=True)
        return node

    def parse_macro(self):
        node = nodes.Macro(lineno=self.stream.expect('macro').lineno)
        node.name = self.stream.expect('name').value
        # make sure that assignments to that name are allowed
        if not nodes.Name(node.name, 'store').can_assign():
            raise TemplateSyntaxError('can\'t assign macro to %r' %
                                      node.target, node.lineno,
                                      self.filename)
        self.parse_signature(node)
        node.body = self.parse_statements(('endmacro',), drop_needle=True)
        return node

    def parse_print(self):
        node = nodes.Output(lineno=self.stream.expect('print').lineno)
        node.nodes = []
        while self.stream.current.type not in statement_end_tokens:
            if node.nodes:
                self.stream.expect('comma')
            node.nodes.append(self.parse_expression())
        return node

    def parse_expression(self, no_condexpr=False):
        """Parse an expression."""
        if no_condexpr:
            return self.parse_or()
        return self.parse_condexpr()

    def parse_condexpr(self):
        lineno = self.stream.current.lineno
        expr1 = self.parse_or()
        while self.stream.current.type is 'if':
            self.stream.next()
            expr2 = self.parse_or()
            self.stream.expect('else')
            expr3 = self.parse_condexpr()
            expr1 = nodes.CondExpr(expr2, expr1, expr3, lineno=lineno)
            lineno = self.stream.current.lineno
        return expr1

    def parse_or(self):
        lineno = self.stream.current.lineno
        left = self.parse_and()
        while self.stream.current.type is 'or':
            self.stream.next()
            right = self.parse_and()
            left = nodes.Or(left, right, lineno=lineno)
            lineno = self.stream.current.lineno
        return left

    def parse_and(self):
        lineno = self.stream.current.lineno
        left = self.parse_compare()
        while self.stream.current.type is 'and':
            self.stream.next()
            right = self.parse_compare()
            left = nodes.And(left, right, lineno=lineno)
            lineno = self.stream.current.lineno
        return left

    def parse_compare(self):
        lineno = self.stream.current.lineno
        expr = self.parse_add()
        ops = []
        while 1:
            token_type = self.stream.current.type
            if token_type in _compare_operators:
                self.stream.next()
                ops.append(nodes.Operand(token_type, self.parse_add()))
            elif token_type is 'not' and self.stream.look().type is 'in':
                self.stream.skip(2)
                ops.append(nodes.Operand('notin', self.parse_add()))
            else:
                break
            lineno = self.stream.current.lineno
        if not ops:
            return expr
        return nodes.Compare(expr, ops, lineno=lineno)

    def parse_add(self):
        lineno = self.stream.current.lineno
        left = self.parse_sub()
        while self.stream.current.type is 'add':
            self.stream.next()
            right = self.parse_sub()
            left = nodes.Add(left, right, lineno=lineno)
            lineno = self.stream.current.lineno
        return left

    def parse_sub(self):
        lineno = self.stream.current.lineno
        left = self.parse_concat()
        while self.stream.current.type is 'sub':
            self.stream.next()
            right = self.parse_concat()
            left = nodes.Sub(left, right, lineno=lineno)
            lineno = self.stream.current.lineno
        return left

    def parse_concat(self):
        lineno = self.stream.current.lineno
        args = [self.parse_mul()]
        while self.stream.current.type is 'tilde':
            self.stream.next()
            args.append(self.parse_mul())
        if len(args) == 1:
            return args[0]
        return nodes.Concat(args, lineno=lineno)

    def parse_mul(self):
        lineno = self.stream.current.lineno
        left = self.parse_div()
        while self.stream.current.type is 'mul':
            self.stream.next()
            right = self.parse_div()
            left = nodes.Mul(left, right, lineno=lineno)
            lineno = self.stream.current.lineno
        return left

    def parse_div(self):
        lineno = self.stream.current.lineno
        left = self.parse_floordiv()
        while self.stream.current.type is 'div':
            self.stream.next()
            right = self.parse_floordiv()
            left = nodes.Div(left, right, lineno=lineno)
            lineno = self.stream.current.lineno
        return left

    def parse_floordiv(self):
        lineno = self.stream.current.lineno
        left = self.parse_mod()
        while self.stream.current.type is 'floordiv':
            self.stream.next()
            right = self.parse_mod()
            left = nodes.FloorDiv(left, right, lineno=lineno)
            lineno = self.stream.current.lineno
        return left

    def parse_mod(self):
        lineno = self.stream.current.lineno
        left = self.parse_pow()
        while self.stream.current.type is 'mod':
            self.stream.next()
            right = self.parse_pow()
            left = nodes.Mod(left, right, lineno=lineno)
            lineno = self.stream.current.lineno
        return left

    def parse_pow(self):
        lineno = self.stream.current.lineno
        left = self.parse_unary()
        while self.stream.current.type is 'pow':
            self.stream.next()
            right = self.parse_unary()
            left = nodes.Pow(left, right, lineno=lineno)
            lineno = self.stream.current.lineno
        return left

    def parse_unary(self):
        token_type = self.stream.current.type
        lineno = self.stream.current.lineno
        if token_type is 'not':
            self.stream.next()
            node = self.parse_unary()
            return nodes.Not(node, lineno=lineno)
        if token_type is 'sub':
            self.stream.next()
            node = self.parse_unary()
            return nodes.Neg(node, lineno=lineno)
        if token_type is 'add':
            self.stream.next()
            node = self.parse_unary()
            return nodes.Pos(node, lineno=lineno)
        return self.parse_primary()

    def parse_primary(self, parse_postfix=True):
        token = self.stream.current
        if token.type is 'name':
            if token.value in ('true', 'false'):
                node = nodes.Const(token.value == 'true', lineno=token.lineno)
            elif token.value == 'none':
                node = nodes.Const(None, lineno=token.lineno)
            else:
                node = nodes.Name(token.value, 'load', lineno=token.lineno)
            self.stream.next()
        elif token.type in ('integer', 'float', 'string'):
            self.stream.next()
            node = nodes.Const(token.value, lineno=token.lineno)
        elif token.type is 'lparen':
            self.stream.next()
            node = self.parse_tuple()
            self.stream.expect('rparen')
        elif token.type is 'lbracket':
            node = self.parse_list()
        elif token.type is 'lbrace':
            node = self.parse_dict()
        else:
            raise TemplateSyntaxError("unexpected token '%s'" %
                                      (token,), token.lineno,
                                      self.filename)
        if parse_postfix:
            node = self.parse_postfix(node)
        return node

    def parse_tuple(self, enforce=False, simplified=False, no_condexpr=False):
        """
        Parse multiple expressions into a tuple. This can also return
        just one expression which is not a tuple. If you want to enforce
        a tuple, pass it enforce=True (currently unused).
        """
        lineno = self.stream.current.lineno
        if simplified:
            parse = self.parse_primary
        elif no_condexpr:
            parse = lambda: self.parse_expression(no_condexpr=True)
        else:
            parse = self.parse_expression
        args = []
        is_tuple = False
        while 1:
            if args:
                self.stream.expect('comma')
            if self.stream.current.type in _tuple_edge_tokens:
                break
            args.append(parse())
            if self.stream.current.type is 'comma':
                is_tuple = True
            else:
                break
            lineno = self.stream.current.lineno
        if not is_tuple and args:
            if enforce:
                raise TemplateSyntaxError('tuple expected', lineno,
                                          self.filename)
            return args[0]
        return nodes.Tuple(args, 'load', lineno=lineno)

    def parse_list(self):
        token = self.stream.expect('lbracket')
        items = []
        while self.stream.current.type is not 'rbracket':
            if items:
                self.stream.expect('comma')
            if self.stream.current.type == 'rbracket':
                break
            items.append(self.parse_expression())
        self.stream.expect('rbracket')
        return nodes.List(items, lineno=token.lineno)

    def parse_dict(self):
        token = self.stream.expect('lbrace')
        items = []
        while self.stream.current.type is not 'rbrace':
            if items:
                self.stream.expect('comma')
            if self.stream.current.type == 'rbrace':
                break
            key = self.parse_expression()
            self.stream.expect('colon')
            value = self.parse_expression()
            items.append(nodes.Pair(key, value, lineno=key.lineno))
        self.stream.expect('rbrace')
        return nodes.Dict(items, lineno=token.lineno)

    def parse_postfix(self, node):
        while 1:
            token_type = self.stream.current.type
            if token_type is 'dot' or token_type is 'lbracket':
                node = self.parse_subscript(node)
            elif token_type is 'lparen':
                node = self.parse_call(node)
            elif token_type is 'pipe':
                node = self.parse_filter(node)
            elif token_type is 'is':
                node = self.parse_test(node)
            else:
                break
        return node

    def parse_subscript(self, node):
        token = self.stream.next()
        if token.type is 'dot':
            attr_token = self.stream.current
            if attr_token.type not in ('name', 'integer'):
                raise TemplateSyntaxError('expected name or number',
                                          attr_token.lineno, self.filename)
            arg = nodes.Const(attr_token.value, lineno=attr_token.lineno)
            self.stream.next()
        elif token.type is 'lbracket':
            args = []
            while self.stream.current.type is not 'rbracket':
                if args:
                    self.stream.expect('comma')
                args.append(self.parse_subscribed())
            self.stream.expect('rbracket')
            if len(args) == 1:
                arg = args[0]
            else:
                arg = nodes.Tuple(args, lineno, self.filename)
        else:
            raise TemplateSyntaxError('expected subscript expression',
                                      self.lineno, self.filename)
        return nodes.Subscript(node, arg, 'load', lineno=token.lineno)

    def parse_subscribed(self):
        lineno = self.stream.current.lineno

        if self.stream.current.type is 'colon':
            self.stream.next()
            args = [None]
        else:
            node = self.parse_expression()
            if self.stream.current.type is not 'colon':
                return node
            self.stream.next()
            args = [node]

        if self.stream.current.type is 'colon':
            args.append(None)
        elif self.stream.current.type not in ('rbracket', 'comma'):
            args.append(self.parse_expression())
        else:
            args.append(None)

        if self.stream.current.type is 'colon':
            self.stream.next()
            if self.stream.current.type not in ('rbracket', 'comma'):
                args.append(self.parse_expression())
            else:
                args.append(None)
        else:
            args.append(None)

        return nodes.Slice(lineno=lineno, *args)

    def parse_call(self, node):
        token = self.stream.expect('lparen')
        args = []
        kwargs = []
        dyn_args = dyn_kwargs = None
        require_comma = False

        def ensure(expr):
            if not expr:
                raise TemplateSyntaxError('invalid syntax for function '
                                          'call expression', token.lineno,
                                          self.filename)

        while self.stream.current.type is not 'rparen':
            if require_comma:
                self.stream.expect('comma')
                # support for trailing comma
                if self.stream.current.type is 'rparen':
                    break
            if self.stream.current.type is 'mul':
                ensure(dyn_args is None and dyn_kwargs is None)
                self.stream.next()
                dyn_args = self.parse_expression()
            elif self.stream.current.type is 'pow':
                ensure(dyn_kwargs is None)
                self.stream.next()
                dyn_kwargs = self.parse_expression()
            else:
                ensure(dyn_args is None and dyn_kwargs is None)
                if self.stream.current.type is 'name' and \
                    self.stream.look().type is 'assign':
                    key = self.stream.current.value
                    self.stream.skip(2)
                    value = self.parse_expression()
                    kwargs.append(nodes.Keyword(key, value,
                                                lineno=value.lineno))
                else:
                    ensure(not kwargs)
                    args.append(self.parse_expression())

            require_comma = True
        self.stream.expect('rparen')

        if node is None:
            return args, kwargs, dyn_args, dyn_kwargs
        return nodes.Call(node, args, kwargs, dyn_args, dyn_kwargs,
                          lineno=token.lineno)

    def parse_filter(self, node, start_inline=False):
        lineno = self.stream.current.type
        while self.stream.current.type == 'pipe' or start_inline:
            if not start_inline:
                self.stream.next()
            token = self.stream.expect('name')
            if self.stream.current.type is 'lparen':
                args, kwargs, dyn_args, dyn_kwargs = self.parse_call(None)
            else:
                args = []
                kwargs = []
                dyn_args = dyn_kwargs = None
            node = nodes.Filter(node, token.value, args, kwargs, dyn_args,
                                dyn_kwargs, lineno=token.lineno)
            start_inline = False
        return node

    def parse_test(self, node):
        token = self.stream.expect('is')
        if self.stream.current.type is 'not':
            self.stream.next()
            negated = True
        else:
            negated = False
        name = self.stream.expect('name').value
        dyn_args = dyn_kwargs = None
        kwargs = []
        if self.stream.current.type is 'lparen':
            args, kwargs, dyn_args, dyn_kwargs = self.parse_call(None)
        elif self.stream.current.type in ('name', 'string', 'integer',
                                          'float', 'lparen', 'lbracket',
                                          'lbrace', 'regex'):
            args = [self.parse_expression()]
        else:
            args = []
        node = nodes.Test(node, name, args, kwargs, dyn_args,
                          dyn_kwargs, lineno=token.lineno)
        if negated:
            node = nodes.Not(node, lineno=token.lineno)
        return node

    def subparse(self, end_tokens=None):
        body = []
        data_buffer = []
        add_data = data_buffer.append

        def flush_data():
            if data_buffer:
                lineno = data_buffer[0].lineno
                body.append(nodes.Output(data_buffer[:], lineno=lineno))
                del data_buffer[:]

        while self.stream:
            token = self.stream.current
            if token.type is 'data':
                if token.value:
                    add_data(nodes.Const(token.value, lineno=token.lineno))
                self.stream.next()
            elif token.type is 'variable_begin':
                self.stream.next()
                add_data(self.parse_tuple())
                self.stream.expect('variable_end')
            elif token.type is 'block_begin':
                flush_data()
                self.stream.next()
                if end_tokens is not None and \
                   self.stream.current.test_many(end_tokens):
                    return body
                body.append(self.parse_statement())
                self.stream.expect('block_end')
            else:
                raise AssertionError('internal parsing error')

        flush_data()
        return body

    def parse(self):
        """Parse the whole template into a `Template` node."""
        result = nodes.Template(self.subparse(), lineno=1)
        result.set_environment(self.environment)
        return result
