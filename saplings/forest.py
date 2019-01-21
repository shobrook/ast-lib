#########
# GLOBALS
#########


import ast
import utils
from collections import defaultdict


######
# MAIN
######


# [] Fix elif/else/except context handling
# [] Handle For and With contexts
# [] Handle comprehensions and generator expressions (ignore assignments, they're too hard)
# [] Handle assignments to data structures
# [] Infer input and return (or yield) types of user-defined functions (and classes)
# [] Inside funcs, block searching parent contexts for aliases equivalent to parameter names (unless it's self)
# [] Get rid of the type/type/type/... schema for searching nodes
# [] Debug the frequency analysis

# Handling Comprehensions #
# - You don't know the length of the resulting data structure
# - Say x = [bar(y) for y in z]; we can say that any instance of x[WHATEVER] corresponds to an instance of bar()

# Handling Functions #
# - If a token is imported, and then a function with the same name is defined, delete the alias for that token
# - Save defined function names and their return types in a field (search field when processing tokens)
# - Is processing the input types of functions as simple as adding the parameter name as an alias for the input node,
#   then calling self.visit() on the function node? You could only modify the input node and it's children, cuz everything
#   else in the function body has already been processed.

# Handling List/Dict/Tuple/Set Assignments #
# - Only care about first-level data structures, not those nested in other nodes
# - Store processed key/val pairs in a global that the assign handler can access
# - In the assignment handler, check if parent node is data structure – if it is, call process_tokenized_node
#   with an arg passed in that will write

# When you delete an alias after reassignment, delete `alias` subscripts too. i.e. x = [1,2,3], then x is
# reassigned, and then nodes with x[0] and x[1] as aliases won't get deleted but need to be

# BUG: Can't handle [1,2,3, ...][0].foo()

class APIForest(ast.NodeVisitor):
    def __init__(self, tree):
        # IDEA: The entire context/aliasing system can be refactored such that
        # the object holds a mapping from contexts to alive and dead nodes. This
        # could make search more efficient and at the very least make this code
        # easier to read.

        self.tree = tree

        # OPTIMIZE: Turns AST into doubly-linked AST
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                child.parent = node

        self._context_stack = ["global"] # Keeps track of current context
        self.dependency_trees = [] # Holds root nodes of API usage trees

        self._structs = [] # Stores processed key/val pairs for access by the assignment handler
        self._in_conditional = False # Flag for whether inside conditional block or not
        self._conditional_assignment_handlers = defaultdict(lambda: [])

        self._context_to_string = lambda: '.'.join(self._context_stack)

        self.visit(self.tree)

    ## Utilities ##

    def _recursively_process_tokens(self, tokens):
        """
        Takes a list of tokens (types: instance, function, args, or subscript)
        and searches for an equivalent nodes in each parse tree. Once the leaf
        of the path of equivalent nodes has been reached, the algorithm creates
        additional nodes and adds them to the parse tree.

        @param tokens: list of tokenized nodes, each as (token(s), type).

        @return: list of references to parse tree nodes corresponding to the
        tokens.
        """

        node_stack = []
        curr_context = self._context_to_string()

        # Flattens nested tokens
        flattened_tokens = []
        for idx, token in enumerate(tokens):
            content, type = token

            adjunctive_content = []
            hash_nested_content = lambda: hash(str(adjunctive_content)) if adjunctive_content else hash(str(content)) # hash() or id()?
            if type == "args":
                for sub_tokens in content:
                    adjunctive_content.append(self._recursively_process_tokens(sub_tokens))
                content = hash_nested_content()
            elif type == "subscript":
                adjunctive_content = self._recursively_process_tokens(content)
                content = hash_nested_content()
            # elif type == "hashmap":
            #     pass # TODO
            # elif type == "array":
            #     pass # TODO

            flattened_tokens.append((content, type))

        for idx, token in enumerate(flattened_tokens):
            content, type = token
            token_id = utils.stringify_tokenized_nodes(flattened_tokens[:idx + 1])

            type_pattern = adjusted_type = "implicit"
            if type == "args":
                adjusted_id = "call"
            elif type == "subscript":
                adjusted_id = "sub"
            else:
                adjusted_id = content
                adjusted_type = "instance"
                type_pattern = "instance/implicit/module"

            if not idx: # Beginning of iteration; find base token
                for root in self.dependency_trees:
                    matching_node = utils.find_matching_node(
                        subtree=root,
                        id=token_id,
                        type_pattern="instance/module/implicit",
                        context=curr_context
                    )

                    if matching_node: # Found match for base token, pushing to stack
                        matching_node.increment_count()
                        node_stack.append(matching_node)
                        break
            elif node_stack: # Stack exists, continue pushing to it
                matching_node = utils.find_matching_node(
                    subtree=node_stack[-1],
                    id=token_id,
                    type_pattern=type_pattern,
                    context=curr_context
                )

                if matching_node: # Found child node
                    matching_node.increment_count()
                    node_stack.append(matching_node)
                else: # No child node found, creating one
                    child_node = utils.Node(
                        id=adjusted_id,
                        type=adjusted_type,
                        context=curr_context,
                        alias=token_id
                    )

                    node_stack[-1].add_child(child_node)
                    node_stack.append(child_node)
            else: break # Base token doesn't exist, abort processing

        return node_stack

    def _process_assignment(self, target, value):
        """
        @param target:
        @param value:
        """

        curr_context = self._context_to_string()
        parent_context = '.'.join(self._context_stack[:-1])
        tokenized_target = utils.recursively_tokenize_node(target, [])

        targ_matches = self._recursively_process_tokens(tokenized_target) # LHS
        val_matches = self._recursively_process_tokens(value) # RHS

        alias = utils.stringify_tokenized_nodes(tokenized_target)
        add_alias = lambda node: node.add_alias(curr_context, alias)
        del_alias = lambda node: node.del_alias(curr_context, alias)
        del_conditional_alias = lambda node: node.del_alias(parent_context, alias)

        if targ_matches and val_matches: # Known node reassigned to known node (K2 = K1)
            targ_node, val_node = targ_matches[-1], val_matches[-1]

            add_alias(val_node)
            del_alias(targ_node)
            if self._in_conditional: # QUESTION: Do you even need an _in_conditional field?
                self._conditional_assignment_handlers[curr_context].append(
                    lambda: map(del_conditional_alias, [targ_node, val_node])
                )
        elif targ_matches and not val_matches: # Known node reassigned to unknown node (K1 = U1)
            targ_node = targ_matches[-1]

            del_alias(targ_node)
            if self._in_conditional:
                self._conditional_assignment_handlers[curr_context].append(
                    lambda: del_conditional_alias(targ_node)
                )
        elif not targ_matches and val_matches: # Unknown node assigned to known node (U1 = K1)
            val_node = val_matches[-1]

            add_alias(val_node)
            if self._in_conditional:
                self._conditional_assignment_handlers[curr_context].append(
                    lambda: del_conditional_alias(val_node)
                )

    def _process_module(self, module, context, alias_root=True):
        """
        Takes the identifier for a module, sometimes a period-separated string
        of sub-modules, and searches the list of parse trees for a matching
        module. If no match is found, new module nodes are generated and
        appended to self.dependency_trees.

        @param module: identifier for the module.
        @param context: context in which the module is imported.
        @param alias_root: flag for whether a newly created module node should
        be aliased.

        @return: reference to the terminal Node object in the list of
        sub-modules.
        """

        sub_modules = module.split('.') # For module.submodule1.submodule2...
        root_module = sub_modules[0]
        term_node = None

        for root in self.dependency_trees:
            matching_module = utils.find_matching_node(
                subtree=root,
                id=root_module,
                type_pattern="module"
            )

            if matching_module:
                term_node = matching_module
                break

        if not term_node:
            term_node = utils.Node(
                id=root_module,
                type="module",
                context=context,
                alias=root_module if alias_root else '' # For `from X import Y`
            )
            self.dependency_trees.append(term_node)

        for idx in range(len(sub_modules[1:])):
            sub_module = sub_modules[idx + 1]
            sub_module_alias = '.'.join([root_module] + sub_modules[1:idx + 2])

            matching_sub_module = utils.find_matching_node(
                subtree=term_node,
                id=sub_module,
                type_pattern="instance",
                context=None # QUESTION: Should this be context?
            )

            if matching_sub_module:
                term_node = matching_sub_module
            else:
                new_sub_module = utils.Node(
                    id=sub_module,
                    type="instance",
                    context=context,
                    alias=sub_module_alias if alias_root else '' # For `from X.Y import Z`
                )
                term_node.add_child(new_sub_module)
                term_node = new_sub_module

        return term_node

    ## Context Managers ##

    # @utils.context_handler
    # def visit_Global(self, node):
    #     # IDEA: Save pre-state of context_stack, set to ["global"],
    #     # then set back to pre-state
    #     return

    # @utils.context_handler
    # def visit_Nonlocal(self, node):
    #     return

    @utils.context_handler
    def visit_ClassDef(self, node):
        pass

    @utils.context_handler
    def visit_FunctionDef(self, node):
        pass

    def visit_AsyncFunctionDef(self, node):
        self.visit_FunctionDef(node)

    # @utils.context_handler
    # def visit_Lambda(self, node):
    #     return

    ## Control Flow Managers ##

    @utils.conditional_handler
    def visit_If(self, node):
        contexts = []
        for else_node in node.orelse:
            else_context = ''.join(["If", str(else_node.lineno)])
            contexts.append('.'.join(self._context_stack + [else_context]))
            self.visit_If(else_node)

        return contexts

    @utils.conditional_handler
    def visit_Try(self, node):
        contexts = []
        for except_handler in node.handlers:
            except_context = ''.join(["ExceptHandler", str(except_handler.lineno)])
            contexts.append('.'.join(self._context_stack + [except_context]))
            self.visit_ExceptHandler(except_handler)

        return contexts

        # QUESTION: How to handle node.orelse and node.finalbody?

    @utils.conditional_handler
    def visit_ExceptHandler(self, node):
        # TODO: Handle `name` assignments (i.e. except Exception as e)
        return []

    @utils.conditional_handler
    def visit_While(self, node):
        return []

    def visit_For(self, node):
        curr_context = self._context_to_string()

        def del_aliases(target):
            """
            Deletes alias for target node in all contexts.
            """

            tokens = utils.recursively_tokenize_node(target, [])
            alias = utils.stringify_tokenized_nodes(tokens)
            nodes = self._recursively_process_tokens(tokens) # Add a NO INCREMENT flag

            if not nodes:
                return

            nodes[-1].del_alias(curr_context, alias)

        if isinstance(node.target, ast.Name):
            del_aliases(node.target)
        elif isinstance(node.target, ast.Tuple) or isinstance(node.target, ast.List):
            map(del_aliases, node.target.elts)

        tokens = utils.recursively_tokenize_node(node.iter, [])
        self._recursively_process_tokens(tokens)

        for body_node in [node.iter] + node.body + node.orelse:
            try:
                node_name = type(body_node).__name__
                custom_visitor = getattr(self, ''.join(["visit_", node_name]))
                custom_visitor(body_node)
            except AttributeError:
                self.generic_visit(body_node)

        # QUESTION: How to handle node.orelse? Nodes in orelse are executed if
        # the loop finishes normally, rather than via a break statement

    def visit_withitem(self, node):
        def get_nodes(n):
            tokens = utils.recursively_tokenize_node(n, [])
            alias = utils.stringify_tokenized_nodes(tokens)
            nodes = self._recursively_process_tokens(tokens)

            if not nodes:
                return

            return nodes[-1], alias

        if node.optional_vars:
            curr_context = self._context_to_string()

            value_node, value_alias = get_nodes(node.context_expr)
            target_node, target_alias = get_nodes(node.optional_vars) # Name, Tuple, or List

            value_node.add_alias(curr_context, target_alias)
            target_node.del_alias(curr_context, target_alias)

    ## Aliasing Handlers ##

    def visit_Import(self, node):
        # TODO: Ignore `import .X`

        curr_context = self._context_to_string()
        for module in node.names:
            alias = module.asname if module.asname else module.name
            module_leaf_node = self._process_module(
                module=module.name,
                context=curr_context,
                alias_root=not bool(module.asname)
            )

            module_leaf_node.add_alias(curr_context, alias)

    def visit_ImportFrom(self, node):
        # TODO: Ignore `from X import *`
        
        if node.level:
            return

        curr_context = self._context_to_string()
        module_node = self._process_module(
            module=node.module,
            context=curr_context,
            alias_root=False
        )

        for alias in node.names:
            child_exists = False
            alias_id = alias.asname if alias.asname else alias.name

            for child in module_node.children:
                if alias.name == child.id:
                    child_exists = True
                    if not alias_id in child.aliases[curr_context]:
                        child.add_alias(curr_context, alias_id)

                    break

            if not child_exists:
                module_node.add_child(utils.Node(
                    id=alias.name,
                    type="instance",
                    context=curr_context,
                    alias=alias_id
                ))

    def visit_Assign(self, node):
        curr_context = self._context_to_string()

        if isinstance(node.value, ast.Tuple):
            node_tokenizer = lambda elt: utils.recursively_tokenize_node(elt, [])
            values = tuple(map(node_tokenizer, node.value.elts))
        else:
            values = utils.recursively_tokenize_node(node.value, [])

        targets = node.targets if hasattr(node, "targets") else (node.target)
        for target in targets:
            if isinstance(target, ast.Tuple):
                for idx, elt in enumerate(target.elts):
                    if isinstance(values, tuple):
                        self._process_assignment(elt, values[idx])
                    else:
                        self._process_assignment(elt, values)
            elif isinstance(values, tuple):
                for value in values:
                    self._process_assignment(target, value)
            else:
                self._process_assignment(target, values)

    def visit_AnnAssign(self, node):
        self.visit_Assign(node)

    @utils.default_handler
    def visit_Call(self, node):
        tokens = utils.recursively_tokenize_node(node, [])
        nodes = self._recursively_process_tokens(tokens)

        return

    @utils.default_handler
    def visit_Attribute(self, node):
        # You could try searching up the node.parent.parent... path to find
        # out if attribute is inside a call node. If it is, let the call visiter
        # take care of it. If it isn't, then keep doing what you're doing.

        # Also possible that the best way to deal with this is by just having
        # one ast.Load visitor. Look into this more, i.e. what gets covered by
        # ast.Load.

        return

    @utils.default_handler
    def visit_Subscript(self, node):
        return

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Del):
            pass  # TODO: Delete alias from tree
        elif isinstance(node.ctx, ast.Load):
            pass  # TODO: Increment count of node (beware of double counting)

        return

    @utils.default_handler
    def visit_Dict(self, node):
        return

    @utils.default_handler
    def visit_List(self, node):
        return

    @utils.default_handler
    def visit_Tuple(self, node):
        return

    @utils.default_handler
    def visit_Set(self, node):
        return

    def visit_comprehension(self, node):
        self.generic_visit(node) # TEMP