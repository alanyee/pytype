"""Converts pyi files to pickled asts and saves them to disk.

Used to speed up module importing. This is done by loading the ast and
serializing it to disk. Further users only need to read the serialized data from
disk, which is faster to digest than a pyi file.
"""

import collections

from pytype.pytd import pytd
from pytype.pytd import pytd_utils
from pytype.pytd.parse import builtins as pytd_builtins
from pytype.pytd.parse import visitors


class UnrestorableDependencyError(Exception):
  """If a dependency can't be restored in the current state."""

  def __init__(self, error_msg):
    super(UnrestorableDependencyError, self).__init__(error_msg)


class FindClassTypesVisitor(visitors.Visitor):

  def __init__(self):
    super(FindClassTypesVisitor, self).__init__()
    self.class_type_nodes = []

  def EnterClassType(self, n):
    self.class_type_nodes.append(n)

SerializableTupleClass = collections.namedtuple(
    "_", ["ast", "dependencies", "class_type_nodes"])


class SerializableAst(SerializableTupleClass):
  """The data pickled to disk to save an ast.

  Attributes:
    ast: The TypeDeclUnit representing the serialized module.
    dependencies: A list of modules this AST depends on. The modules are
      represented as Fully Qualified names. E.g. foo.bar.module. This set will
      also contain the module being imported, if the module is not empty.
      Therefore it might be different from the set found by
      visitors.CollectDependencies in
      load_pytd._load_and_resolve_ast_dependencies.
    class_type_nodes: A list of all the ClassType instances in ast or None. If
      this list is provided only the ClassType instances in the list will be
      visited and have their .cls set. If this attribute is None the whole AST
      will be visited and all found ClassType instances will have their .cls
      set.
  """
  Replace = SerializableTupleClass._replace  # pylint: disable=no-member,invalid-name


class RenameModuleVisitor(visitors.Visitor):
  """Renames a TypeDeclUnit."""

  def __init__(self, old_module_name, new_module_name):
    """Constructor.

    Args:
      old_module_name: The old name of the module as a string,
        e.g. "foo.bar.module1"
      new_module_name: The new name of the module as a string,
        e.g. "barfoo.module2"

    Raises:
      ValueError: If the old_module name is an empty string.
    """
    super(RenameModuleVisitor, self).__init__()
    if not old_module_name:
      raise ValueError("old_module_name must be a non empty string.")
    assert not old_module_name.endswith(".")
    assert not new_module_name.endswith(".")
    self._module_name = new_module_name
    self._old = old_module_name + "." if old_module_name else ""
    self._new = new_module_name + "." if new_module_name else ""

  def _MaybeNewName(self, name):
    """Decides if a name should be replaced.

    Args:
      name: A name for which a prefix should be changed.

    Returns:
      If name is local to the module described by old_module_name the
      old_module_part will be replaced by new_module_name and returned,
      otherwise node.name will be returned.
    """
    if not name:
      return name
    before, match, after = name.partition(self._old)
    if match and not before and "." not in after:
      return self._new + after
    else:
      return name

  def _ReplaceModuleName(self, node):
    new_name = self._MaybeNewName(node.name)
    if new_name != node.name:
      return node.Replace(name=new_name)
    else:
      return node

  def VisitClassType(self, node):
    new_name = self._MaybeNewName(node.name)
    if new_name != node.name:
      return pytd.ClassType(new_name, node.cls)
    else:
      return node

  def VisitTypeDeclUnit(self, node):
    return node.Replace(name=self._module_name)

  def VisitTypeParameter(self, node):
    new_scope = self._MaybeNewName(node.scope)
    if new_scope != node.scope:
      return node.Replace(scope=new_scope)
    return node

  VisitConstant = _ReplaceModuleName  # pylint: disable=invalid-name
  VisitAlias = _ReplaceModuleName  # pylint: disable=invalid-name
  VisitClass = _ReplaceModuleName  # pylint: disable=invalid-name
  VisitFunction = _ReplaceModuleName  # pylint: disable=invalid-name
  VisitStrictType = _ReplaceModuleName  # pylint: disable=invalid-name
  VisitNamedType = _ReplaceModuleName  # pylint: disable=invalid-name


def StoreAst(ast, filename=None):
  """Loads and stores an ast to disk.

  Args:
    ast: The pytd.TypeDeclUnit to save to disk.
    filename: The filename for the pickled output. If this is None, this
      function instead returns the pickled string.

  Returns:
    The pickled string, if no filename was given. (None otherwise.)
  """
  if ast.name.endswith(".__init__"):
    ast = ast.Visit(RenameModuleVisitor(
        ast.name, ast.name.rsplit(".__init__", 1)[0]))
  # Collect dependencies
  deps = visitors.CollectDependencies()
  ast.Visit(deps)
  dependencies = deps.modules or set()

  # Clean external references
  ast.Visit(visitors.ClearClassPointers())
  indexer = FindClassTypesVisitor()
  ast.Visit(indexer)
  return pytd_utils.SavePickle(SerializableAst(
      ast, list(sorted(dependencies)),
      list(sorted(indexer.class_type_nodes))), filename)


def EnsureAstName(ast, module_name):
  """Rename the serializable_ast if the name is different from module_name.

  Args:
    ast: An instance of SerializableAst.
    module_name: The name under which ast.ast should be loaded.

  Returns:
    The updated SerializableAst.
  """
  # The most likely case is module_name==raw_ast.name .
  raw_ast = ast.ast

  # module_name is the name from this run, raw_ast.name is the guessed name from
  # when the ast has been pickled.
  if module_name != raw_ast.name:
    ast = ast.Replace(class_type_nodes=None)
    ast = ast.Replace(
        ast=raw_ast.Visit(RenameModuleVisitor(raw_ast.name, module_name)))
  return ast


def ProcessAst(serializable_ast, module_map):
  """Postprocess a pickled ast.

  Postprocessing will either just fill the ClassType references from module_map
  or if module_name changed between pickling and loading rename the module
  internal references to the new module_name.
  Renaming is more expensive than filling references, as the whole AST needs to
  be rebuild.

  Args:
    serializable_ast: A SerializableAst instance.
    module_map: Used to resolve ClassType.cls links to already loaded modules.
      The loaded module will be added to the dict.

  Returns:
    A pytd.TypeDeclUnit, this is either the input raw_ast with the references
    set or a newly created AST with the new module_name and the references set.

  Raises:
    AssertionError: If module_name is already in module_map, which means that
      module_name is already loaded.
    UnrestorableDependencyError: If no concrete module exists in module_map for
      one of the references from the pickled ast.
  """
  # Module external and internal references need to be filled in different
  # steps. As a part of a local ClassType referencing an external cls, might be
  # changed structurally, if the external class definition used here is
  # different from the one used during serialization. Changing an attribute
  # (other than .cls) will trigger an recreation of the ClassType in which case
  # we need the reference to the new instance, which can only be known after all
  # external references are resolved.
  serializable_ast = _LookupClassReferences(
      serializable_ast, module_map, serializable_ast.ast.name)
  _FillLocalReferences(serializable_ast, {
      "": serializable_ast.ast,
      serializable_ast.ast.name: serializable_ast.ast})
  return serializable_ast.ast


def _LookupClassReferences(serializable_ast, module_map, self_name):
  """Fills .cls references in serializable_ast.ast with ones from module_map.

  Already filled references are not changed. References to the module self._name
  are not filled. Setting self_name=None will fill all references.

  Args:
    serializable_ast: A SerializableAst instance.
    module_map: Used to resolve ClassType.cls links to already loaded modules.
      The loaded module will be added to the dict.
    self_name: A string representation of a module which should not be resolved,
      for example: "foo.bar.module1" or None to resolve all modules.

  Returns:
    A SerializableAst with an updated .ast. .class_type_nodes is set to None
    if any of the Nodes needed to be regenerated.
  """

  class_lookup = visitors.LookupExternalTypes(module_map, full_names=True,
                                              self_name=self_name)
  raw_ast = serializable_ast.ast

  if serializable_ast.class_type_nodes:
    for node in serializable_ast.class_type_nodes:
      try:
        if node is not class_lookup.VisitClassType(node):
          serializable_ast = serializable_ast.Replace(class_type_nodes=None)
          break
      except KeyError as e:
        raise UnrestorableDependencyError("Unresolved class: %r." % e.message)
  if serializable_ast.class_type_nodes is None:
    try:
      raw_ast = raw_ast.Visit(class_lookup)
    except KeyError as e:
      raise UnrestorableDependencyError("Unresolved class: %r." % e.message)
  serializable_ast = serializable_ast.Replace(ast=raw_ast)
  return serializable_ast


def _FillLocalReferences(serializable_ast, module_map):
  local_filler = visitors.FillInLocalPointers(module_map)
  if serializable_ast.class_type_nodes:
    for node in serializable_ast.class_type_nodes:
      local_filler.EnterClassType(node)
      if node.cls is None:
        raise AssertionError("This should not happen: %s" % str(node))
  else:
    serializable_ast.ast.Visit(local_filler)


def PrepareForExport(module_name, python_version, ast, loader):
  """Prepare an ast as if it was parsed and loaded.

  External dependencies will not be resolved, as the ast generated by this
  method is supposed to be exported.

  Args:
    module_name: The module_name as a string for the returned ast.
    python_version: A tuple of (major, minor) python version as string
      (see config.python_version).
    ast: pytd.TypeDeclUnit, is only used if src is None.
    loader: A load_pytd.Loader instance.

  Returns:
    A pytd.TypeDeclUnit representing the supplied AST as it would look after
    being written to a file and parsed.
  """
  # This is a workaround for functionality which crept into places it doesn't
  # belong. Ideally this would call some transformation Visitors on ast to
  # transform it into the same ast we get after parsing and loading (compare
  # load_pytd.Loader.load_file). Unfortunately parsing has some special cases,
  # e.g. '__init__' return type and '__new__' being a 'staticmethod', which
  # need to be moved to visitors before we can do this. Printing an ast also
  # applies transformations,
  # e.g. visitors.PrintVisitor._FormatContainerContents, which need to move to
  # their own visitors so they can be applied without printing.
  src = pytd_utils.Print(ast)
  ast = pytd_builtins.ParsePyTD(src=src, module=module_name,
                                python_version=python_version)
  ast = ast.Visit(visitors.LookupBuiltins(loader.builtins, full_names=False))
  ast = ast.Visit(visitors.ExpandCompatibleBuiltins(loader.builtins))
  ast = ast.Visit(visitors.LookupLocalTypes())
  ast = ast.Visit(visitors.AdjustTypeParameters())
  ast = ast.Visit(visitors.NamedTypeToClassType())
  ast = ast.Visit(visitors.FillInLocalPointers({"": ast, module_name: ast}))
  ast = ast.Visit(visitors.CanonicalOrderingVisitor())
  ast = ast.Visit(visitors.ClassTypeToLateType(
      ignore=[module_name + ".", "__builtin__.", "typing."]))
  return ast
