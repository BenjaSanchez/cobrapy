"""
SBML import and export using python-libsbml(-experimental).

- The SBML importer supports all versions of SBML and the fbc package.
- The SBML exporter writes SBML L3 models.
- Annotation information is stored on the cobrapy objects
- Information from the group package is read

Parsing of fbc models was implemented as efficient as possible, whereas
(discouraged) fallback solutions are not optimized for efficiency.

Notes are only supported in a minimal way relevant for constraint-based
models. I.e., structured information from notes in the form
   <p>key: value</p>
is read into the Object.notes dictionary when reading SBML files.
On writing the Object.notes dictionary is serialized to the SBML
notes information.

Annotations are read in the Object.annotation fields.

Some SBML related issues are still open, please refer to the respective issue:
- fix incorrect boundary conditions (depends on decision on how to handle
    boundary conditions; https://github.com/opencobra/cobrapy/issues/811)
- update annotation format and support qualifiers (depends on decision
    for new annotation format; https://github.com/opencobra/cobrapy/issues/684)
- write compartment annotations and notes (depends on updated first-class
    compartments; see https://github.com/opencobra/cobrapy/issues/760)
- support compression on file handles (depends on solution for
    https://github.com/opencobra/cobrapy/issues/812)
"""

from __future__ import absolute_import

import os
import re
import traceback
import logging
from warnings import catch_warnings, simplefilter
from six import string_types, iteritems
from collections import defaultdict, namedtuple
from copy import deepcopy

import libsbml
import cobra
from cobra.core import Gene, Metabolite, Model, Reaction, Group
from cobra.util.solver import set_objective, linear_reaction_coefficients
from cobra.manipulation.validate import check_metabolite_compartment_formula


class CobraSBMLError(Exception):
    """ SBML error class. """
    pass


LOGGER = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Defaults and constants for writing SBML
# -----------------------------------------------------------------------------
config = cobra.Configuration()
LOWER_BOUND = config.lower_bound
UPPER_BOUND = config.upper_bound
LOWER_BOUND_ID = "cobra_default_lb"
UPPER_BOUND_ID = "cobra_default_ub"
ZERO_BOUND_ID = "cobra_0_bound"

SBO_FBA_FRAMEWORK = "SBO:0000624"
SBO_DEFAULT_FLUX_BOUND = "SBO:0000626"
SBO_FLUX_BOUND = "SBO:0000625"

LONG_SHORT_DIRECTION = {'maximize': 'max', 'minimize': 'min'}
SHORT_LONG_DIRECTION = {'min': 'minimize', 'max': 'maximize'}

Unit = namedtuple('Unit', ['kind', 'scale', 'multiplier', 'exponent'])
UNITS_FLUX = ("mmol_per_gDW_per_hr",
              [
                  Unit(kind=libsbml.UNIT_KIND_MOLE, scale=-3, multiplier=1,
                       exponent=1),
                  Unit(kind=libsbml.UNIT_KIND_GRAM, scale=0, multiplier=1,
                       exponent=-1),
                  Unit(kind=libsbml.UNIT_KIND_SECOND, scale=0, multiplier=3600,
                       exponent=-1)
              ])

# -----------------------------------------------------------------------------
# Functions for id replacements (import/export)
# -----------------------------------------------------------------------------
SBML_DOT = "__SBML_DOT__"


def _clip(sid, prefix):
    """Clips a prefix from the beginning of a string if it exists."""
    return sid[len(prefix):] if sid.startswith(prefix) else sid


def _f_gene(sid, prefix="G_"):
    """Clips gene prefix from id."""
    sid = sid.replace(SBML_DOT, ".")
    return _clip(sid, prefix)


def _f_gene_rev(sid, prefix="G_"):
    """Adds gene prefix to id."""
    return prefix + sid.replace(".", SBML_DOT)


def _f_specie(sid, prefix="M_"):
    """Clips specie/metabolite prefix from id."""
    return _clip(sid, prefix)


def _f_specie_rev(sid, prefix="M_"):
    """Adds specie/metabolite prefix to id."""
    return prefix + sid


def _f_reaction(sid, prefix="R_"):
    """Clips reaction prefix from id."""
    return _clip(sid, prefix)


def _f_reaction_rev(sid, prefix="R_"):
    """Adds reaction prefix to id."""
    return prefix + sid


F_GENE = "F_GENE"
F_GENE_REV = "F_GENE_REV"
F_SPECIE = "F_SPECIE"
F_SPECIE_REV = "F_SPECIE_REV"
F_REACTION = "F_REACTION"
F_REACTION_REV = "F_REACTION_REV"

F_REPLACE = {
    F_GENE: _f_gene,
    F_GENE_REV: _f_gene_rev,
    F_SPECIE: _f_specie,
    F_SPECIE_REV: _f_specie_rev,
    F_REACTION: _f_reaction,
    F_REACTION_REV: _f_reaction_rev,
}


# ----------------------
# Read SBML
# ----------------------
def read_sbml_model(filename, number=float, f_replace=F_REPLACE, **kwargs):
    """Reads SBML model from given filename.

    If the given filename ends with the suffix ''.gz'' (for example,
    ''myfile.xml.gz'),' the file is assumed to be compressed in gzip
    format and will be automatically decompressed upon reading. Similarly,
    if the given filename ends with ''.zip'' or ''.bz2',' the file is
    assumed to be compressed in zip or bzip2 format (respectively).  Files
    whose names lack these suffixes will be read uncompressed.  Note that
    if the file is in zip format but the archive contains more than one
    file, only the first file in the archive will be read and the rest
    ignored.

    To read a gzip/zip file, libSBML needs to be configured and linked
    with the zlib library at compile time.  It also needs to be linked
    with the bzip2 library to read files in bzip2 format.  (Both of these
    are the default configurations for libSBML.)

    This function supports SBML with FBC-v1 and FBC-v2. FBC-v1 models
    are converted to FBC-v2 models before reading.

    The parser tries to fall back to information in notes dictionaries
    if information is not available in the FBC packages, e.g.,
    CHARGE, FORMULA on species, or GENE_ASSOCIATION, SUBSYSTEM on reactions.

    Parameters
    ----------
    filename : path to SBML file, or SBML string, or SBML file handle
        SBML which is read into cobra model
    number: data type of stoichiometry: {float, int}
        In which data type should the stoichiometry be parsed.
    f_replace : dict of replacement functions for id replacement
        Dictionary of replacement functions for gene, specie, and reaction.
        By default the following id changes are performed on import:
        clip G_ from genes, clip M_ from species, clip R_ from reactions
        If no replacements should be performed, set f_replace={}, None

    Returns
    -------
    cobra.core.Model

    Notes
    -----
    Provided file handles cannot be opened in binary mode, i.e., use
        with open(path, "r" as f):
            read_sbml_model(f)
    File handles to compressed files are not supported yet.
    """
    try:
        doc = _get_doc_from_filename(filename)
        return _sbml_to_model(doc, number=number,
                              f_replace=f_replace, **kwargs)
    except Exception:
        print(traceback.print_exc())
        raise CobraSBMLError(
            "Something went wrong reading the SBML model. You can get a "
            "detailed report using the `cobra.io.sbml.validate_sbml_model` "
            "function or using the online validator at "
            "http://sbml.org/validator")


def _get_doc_from_filename(filename):
    """Get SBMLDocument from given filename.

    Parameters
    ----------
    filename : path to SBML, or SBML string, or filehandle

    Returns
    -------
    libsbml.SBMLDocument
    """
    if isinstance(filename, string_types):
        if os.path.exists(filename):
            # path
            doc = libsbml.readSBMLFromFile(filename)  # noqa: E501 type: libsbml.SBMLDocument
        else:
            # string representation
            doc = libsbml.readSBMLFromString(filename)  # noqa: E501 type: libsbml.SBMLDocument

    elif hasattr(filename, "read"):
        # file handle
        doc = libsbml.readSBMLFromString(filename.read())  # noqa: E501 type: libsbml.SBMLDocument
    else:
        raise CobraSBMLError("Input format for 'filename' is not supported.")

    return doc


def _sbml_to_model(doc, number=float, f_replace=None, **kwargs):
    """Creates cobra model from SBMLDocument.

    Parameters
    ----------
    doc: libsbml.SBMLDocument
    number: data type of stoichiometry: {float, int}
        In which data type should the stoichiometry be parsed.
    f_replace : dict of replacement functions for id replacement

    Returns
    -------
    cobra.core.Model
    """
    if f_replace is None:
        f_replace = {}

    # SBML model
    model = doc.getModel()  # type: libsbml.Model
    if model is None:
        raise CobraSBMLError("No SBML model detected in file.")
    model_fbc = model.getPlugin("fbc")  # type: libsbml.FbcModelPlugin

    if not model_fbc:
        LOGGER.warning("Model does not contain FBC information.")
    else:
        if not model_fbc.isSetStrict():
            LOGGER.warning('Loading SBML model without fbc:strict="true"')

        # fbc-v1 (legacy)
        doc_fbc = doc.getPlugin("fbc")  # type: libsbml.FbcSBMLDocumentPlugin
        fbc_version = doc_fbc.getPackageVersion()

        if fbc_version == 1:
            LOGGER.warning("Loading SBML with fbc-v1 (models should be encoded"
                           " using fbc-v2)")
            conversion_properties = libsbml.ConversionProperties()
            conversion_properties.addOption("convert fbc v1 to fbc v2", True,
                                            "Convert FBC-v1 model to FBC-v2")
            result = doc.convert(conversion_properties)
            if result != libsbml.LIBSBML_OPERATION_SUCCESS:
                raise Exception("Conversion of SBML fbc v1 to fbc v2 failed")

    # Model
    cobra_model = Model(model.getId())
    cobra_model.name = model.getName()

    # meta information
    meta = {
        "model.id": model.getId(),
        "level": model.getLevel(),
        "version": model.getVersion(),
        "packages": []
    }
    # History
    creators = []
    created = None
    if model.isSetModelHistory():
        history = model.getModelHistory()  # type: libsbml.ModelHistory

        if history.isSetCreatedDate():
            created = history.getCreatedDate()

        for c in history.getListCreators():  # type: libsbml.ModelCreator
            creators.append({
                "familyName": c.getFamilyName() if c.isSetFamilyName() else None,  # noqa: E501
                "givenName": c.getGivenName() if c.isSetGivenName() else None,
                "organisation": c.getOrganisation() if c.isSetOrganisation() else None,  # noqa: E501
                "email": c.getEmail() if c.isSetEmail() else None,
            })

    meta["creators"] = creators
    meta["created"] = created
    meta["notes"] = _parse_notes_dict(doc)
    meta["annotation"] = _parse_annotations(doc)

    info = "<{}> SBML L{}V{}".format(model.getId(),
                                     model.getLevel(), model.getVersion())
    packages = {}
    for k in range(doc.getNumPlugins()):
        plugin = doc.getPlugin(k)  # type:libsbml.SBasePlugin
        key, value = plugin.getPackageName(), plugin.getPackageVersion()
        packages[key] = value
        info += ", {}-v{}".format(key, value)
        if key not in ["fbc", "groups"]:
            LOGGER.warning("SBML package '{}' not supported by cobrapy,"
                           "information is not parsed".format(key))
    meta["info"] = info
    meta["packages"] = packages
    cobra_model._sbml = meta
    # print("READ", cobra_model._sbml["info"])

    # notes and annotations
    cobra_model.notes = _parse_notes_dict(model)
    cobra_model.annotation = _parse_annotations(model)

    # Compartments
    # FIXME: update with new compartments
    cobra_model.compartments = {c.getId(): c.getName()
                                for c in model.getListOfCompartments()}

    # Species
    boundary_ids = set()
    metabolites = []
    for s in model.getListOfSpecies():  # type: libsbml.Species
        sid = _check_required(s, s.getId(), "id")
        if f_replace and F_SPECIE in f_replace:
            sid = f_replace[F_SPECIE](sid)

        met = Metabolite(sid)
        met.name = s.getName()
        met.notes = _parse_notes_dict(s)
        met.annotation = _parse_annotations(s)
        met.compartment = s.getCompartment()

        s_fbc = s.getPlugin("fbc")  # type: libsbml.FbcSpeciesPlugin
        if s_fbc:
            met.charge = s_fbc.getCharge()
            met.formula = s_fbc.getChemicalFormula()
        else:
            if s.isSetCharge():
                LOGGER.warning("Use of charge attribute is "
                               "discouraged '%s, use fbc:charge "
                               "instead." % s)
                met.charge = s.getCharge()
            else:
                if 'CHARGE' in met.notes:
                    LOGGER.warning("Use of CHARGE note is discouraged '%s, "
                                   "use fbc:charge instead." % s)
                    try:
                        met.charge = int(met.notes['CHARGE'])
                    except ValueError:
                        # handle nan, na, NA, ...
                        pass

            if 'FORMULA' in met.notes:
                LOGGER.warning("Use of FORMULA note is discouraged '%s, "
                               "use fbc:chemicalFormula instead." % s)
                met.formula = met.notes['FORMULA']

        # Detect boundary metabolites - In case they have been mistakenly
        # added. They should not actually appear in a model
        # FIXME: This is incorrect behavior
        #  (https://github.com/opencobra/cobrapy/issues/811)
        if s.getBoundaryCondition() is True:
            boundary_ids.add(s.getId())

        metabolites.append(met)

    cobra_model.add_metabolites(metabolites)

    # Genes
    if model_fbc:
        for gp in model_fbc.getListOfGeneProducts():  # noqa: E501 type: libsbml.GeneProduct
            gid = gp.getId()
            if f_replace and F_GENE in f_replace:
                gid = f_replace[F_GENE](gid)
            gene = Gene(gid)
            gene.name = gp.getName()
            if gene.name is None:
                gene.name = gid
            gene.annotation = _parse_annotations(gp)
            gene.notes = _parse_notes_dict(gp)

            cobra_model.genes.append(gene)
    else:
        for reaction in model.getListOfReactions():  # noqa: E501 type: libsbml.Reaction
            # fallback to notes information

            notes = _parse_notes_dict(reaction)
            if "GENE ASSOCIATION" in notes:
                gpr = notes['GENE ASSOCIATION']
            elif "GENE_ASSOCIATION" in notes:
                gpr = notes['GENE_ASSOCIATION']
            else:
                gpr = ''

            if len(gpr) > 0:
                gpr = gpr.replace("(", ";")
                gpr = gpr.replace(")", ";")
                gpr = gpr.replace("or", ";")
                gpr = gpr.replace("and", ";")
                gids = [t.strip() for t in gpr.split(';')]
                # create missing genes
                for gid in gids:
                    if f_replace and F_GENE in f_replace:
                        gid = f_replace[F_GENE](gid)

                    if gid not in cobra_model.genes:
                        gene = Gene(gid)
                        gene.name = gid
                        cobra_model.genes.append(gene)

    # GPR rules
    def process_association(ass):
        """ Recursively convert gpr association to a gpr string. """
        if ass.isFbcOr():
            return " ".join(
                ["(", ' or '.join(process_association(c)
                                  for c in ass.getListOfAssociations()), ")"]
            )
        elif ass.isFbcAnd():
            return " ".join(
                ["(", ' and '.join(process_association(c)
                                   for c in ass.getListOfAssociations()), ")"])
        elif ass.isGeneProductRef():
            gid = ass.getGeneProduct()
            if f_replace and F_GENE in f_replace:
                return f_replace[F_GENE](gid)
            else:
                return gid

    # Reactions
    reactions = []
    for r in model.getListOfReactions():  # type: libsbml.Reaction
        rid = _check_required(r, r.getId(), "id")
        if f_replace and F_REACTION in f_replace:
            rid = f_replace[F_REACTION](rid)
        reaction = Reaction(rid)
        reaction.name = r.getName()
        reaction.annotation = _parse_annotations(r)
        reaction.notes = _parse_notes_dict(r)

        # set bounds
        r_fbc = r.getPlugin("fbc")  # type: libsbml.FbcReactionPlugin
        if r_fbc:
            # bounds in fbc
            lb_id = _check_required(r_fbc, r_fbc.getLowerFluxBound(),
                                    "lowerFluxBound")
            ub_id = _check_required(r_fbc, r_fbc.getUpperFluxBound(),
                                    "upperFluxBound")
            p_lb = model.getParameter(lb_id)  # type: libsbml.Parameter
            p_ub = model.getParameter(ub_id)  # type: libsbml.Parameter

            if p_lb.getConstant() and (p_lb.getValue() is not None):
                reaction.lower_bound = p_lb.getValue()
            else:
                raise CobraSBMLError("No constant bound '%s' for "
                                     "reaction '%s" % (p_lb, r))

            if p_ub.getConstant() and (p_ub.getValue() is not None):
                reaction.upper_bound = p_ub.getValue()
            else:
                raise CobraSBMLError("No constant bound '%s' for "
                                     "reaction '%s" % (p_ub, r))

        elif r.isSetKineticLaw():
            # some legacy models encode bounds in kinetic laws
            klaw = r.getKineticLaw()  # type: libsbml.KineticLaw
            p_lb = klaw.getParameter("LOWER_BOUND")  # noqa: E501 type: libsbml.LocalParameter
            if p_lb:
                reaction.lower_bound = p_lb.getValue()
            else:
                raise CobraSBMLError("Missing flux bounds on reaction %s" % r)
            p_ub = klaw.getParameter("UPPER_BOUND")  # noqa: E501 type: libsbml.LocalParameter
            if p_ub:
                reaction.upper_bound = p_ub.getValue()
            else:
                raise CobraSBMLError("Missing flux bounds on reaction %s" % r)

            LOGGER.warning("Encoding LOWER_BOUND and UPPER_BOUND in "
                           "KineticLaw is discouraged '%s, "
                           "use fbc:fluxBounds instead." % r)

        else:
            raise CobraSBMLError("No flux bounds on reaction '%s'" % r)

        reactions.append(reaction)

        # parse equation
        stoichiometry = defaultdict(lambda: 0)
        for sref in r.getListOfReactants():  # type: libsbml.SpeciesReference
            sid = sref.getSpecies()
            if f_replace and F_SPECIE in f_replace:
                sid = f_replace[F_SPECIE](sid)
            stoichiometry[sid] -= number(
                _check_required(sref, sref.getStoichiometry(),
                                "stoichiometry"))

        for sref in r.getListOfProducts():  # type: libsbml.SpeciesReference
            sid = sref.getSpecies()
            if f_replace and F_SPECIE in f_replace:
                sid = f_replace[F_SPECIE](sid)
            stoichiometry[sid] += number(
                _check_required(sref, sref.getStoichiometry(),
                                "stoichiometry"))

        # needs to have keys of metabolite objects, not ids
        object_stoichiometry = {}
        for met_id in stoichiometry:

            # FIXME: This is incorrect behavior, boundary species must be
            #   created and additional exchange reaction must be added.
            #   (https://github.com/opencobra/cobrapy/issues/811)
            if met_id in boundary_ids:
                LOGGER.warning("Boundary metabolite '%s' used in "
                               "reaction '%s'" % (met_id, reaction.getId()))
                continue
            try:
                metabolite = cobra_model.metabolites.get_by_id(met_id)
            except KeyError:
                LOGGER.warning("Ignoring unknown metabolite '%s' in "
                               "reaction %s" % (met_id, reaction.getId()))
                continue
            object_stoichiometry[metabolite] = stoichiometry[met_id]
        reaction.add_metabolites(object_stoichiometry)

        # GPR
        if r_fbc:
            gpr = ''
            gpa = r_fbc.getGeneProductAssociation()  # noqa: E501 type: libsbml.GeneProductAssociation
            if gpa is not None:
                association = gpa.getAssociation()  # noqa: E501 type: libsbml.FbcAssociation
                gpr = process_association(association)
        else:
            # fallback to notes information
            notes = reaction.notes
            if "GENE ASSOCIATION" in notes:
                gpr = notes['GENE ASSOCIATION']
            elif "GENE_ASSOCIATION" in notes:
                gpr = notes['GENE_ASSOCIATION']
            else:
                gpr = ''

            if len(gpr) > 0:
                LOGGER.warning("Use of GENE ASSOCIATION note is "
                               "discouraged '%s, use fbc:gpr instead." % r)
                if f_replace and F_GENE in f_replace:
                    gpr = " ".join(f_replace[F_GENE](t) for t in gpr.split(' '))

        # remove outside parenthesis, if any
        if gpr.startswith("(") and gpr.endswith(")"):
            gpr = gpr[1:-1].strip()

        reaction.gene_reaction_rule = gpr

    cobra_model.add_reactions(reactions)

    # Objective
    obj_direction = "max"
    coefficients = {}
    if model_fbc:
        obj_list = model_fbc.getListOfObjectives()  # noqa: E501 type: libsbml.ListOfObjectives
        if obj_list is None:
            LOGGER.warning("listOfObjectives element not found")
        elif obj_list.size() == 0:
            LOGGER.warning("No objective in listOfObjectives")
        elif not obj_list.getActiveObjective():
            LOGGER.warning("No active objective in listOfObjectives")
        else:
            obj_id = obj_list.getActiveObjective()
            obj = model_fbc.getObjective(obj_id)  # type: libsbml.Objective
            obj_direction = LONG_SHORT_DIRECTION[obj.getType()]

            for flux_obj in obj.getListOfFluxObjectives():  # noqa: E501 type: libsbml.FluxObjective
                rid = flux_obj.getReaction()
                if f_replace and F_REACTION in f_replace:
                    rid = f_replace[F_REACTION](rid)
                try:
                    objective_reaction = cobra_model.reactions.get_by_id(rid)
                except KeyError:
                    raise CobraSBMLError("Objective reaction '%s' "
                                         "not found" % rid)
                try:
                    coefficients[objective_reaction] = number(
                        flux_obj.getCoefficient()
                    )
                except ValueError as e:
                    LOGGER.warning(str(e))
            set_objective(cobra_model, coefficients)
            cobra_model.solver.objective.direction = obj_direction
    else:
        # some legacy models encode objective coefficients in kinetic laws
        for reaction in model.getListOfReactions():  # noqa: E501 type: libsbml.Reaction
            if reaction.isSetKineticLaw():

                klaw = r.getKineticLaw()  # type: libsbml.KineticLaw
                p_oc = klaw.getParameter(
                    "OBJECTIVE_COEFFICIENT")  # noqa: E501 type: libsbml.LocalParameter
                if p_oc:
                    rid = reaction.getId()
                    if f_replace and F_REACTION in f_replace:
                        rid = f_replace[F_REACTION](rid)
                    try:
                        objective_reaction = cobra_model.reactions.get_by_id(
                            rid)
                    except KeyError:
                        raise CobraSBMLError("Objective reaction '%s' "
                                             "not found" % rid)
                    try:
                        coefficients[objective_reaction] = number(
                            p_oc.getValue())
                    except ValueError as e:
                        LOGGER.warning(str(e))

                    LOGGER.warning("Encoding OBJECTIVE_COEFFICIENT in "
                                   "KineticLaw is discouraged '%s, "
                                   "use fbc:fluxObjective instead." % reaction)

    set_objective(cobra_model, coefficients)
    cobra_model.solver.objective.direction = obj_direction

    # parse groups
    model_groups = model.getPlugin("groups")  # type: libsbml.GroupsModelPlugin
    groups = []
    if model_groups:
        for g in model_groups.getListOfGroups():  # type: libsbml.Group
            group = Group(g.getId())
            group.name = g.getName()
            if g.isSetKind():
                group.kind = g.getKindAsString()
            group.annotation = _parse_annotations(g)
            group.notes = _parse_notes_dict(g)

            members = []
            for m in g.getListOfMembers():  # type: libsbml.Member
                if m.isSetIdRef():
                    obj = doc.getElementBySId(m.getIdRef())
                elif m.isSetMetaIdRef():
                    obj = doc.getElementByMetaId(m.getMetaIdRef())

                typecode = obj.getTypeCode()
                obj_id = obj.getId()

                member = None
                if typecode == libsbml.SBML_SPECIES:
                    if f_replace and F_SPECIE in f_replace:
                        obj_id = f_replace[F_SPECIE](obj_id)
                    member = cobra_model.metabolites.get_by_id(obj_id)
                elif typecode == libsbml.SBML_REACTION:
                    if f_replace and F_REACTION in f_replace:
                        obj_id = f_replace[F_REACTION](obj_id)
                    member = cobra_model.reactions.get_by_id(obj_id)
                elif typecode == libsbml.SBML_FBC_GENEPRODUCT:
                    if f_replace and F_GENE in f_replace:
                        obj_id = f_replace[F_GENE](obj_id)
                    member = cobra_model.genes.get_by_id(obj_id)
                else:
                    LOGGER.warning("Member {} could not be added to group {}."
                                   "unsupported type code: {}"
                                   "".format(m, g, typecode, None))

                if member:
                    members.append(member)

            group.add_members(members)
            groups.append(group)
    else:
        # parse deprecated subsystems on reactions
        groups_dict = {}
        for reaction in cobra_model.reactions:
            if "SUBSYSTEM" in reaction.notes:
                g_name = reaction.notes["SUBSYSTEM"]
                if g_name in groups_dict:
                    groups_dict[g_name].append(reaction)
                else:
                    groups_dict[g_name] = [reaction]

        for gid, members in groups_dict.items():
            group = Group(gid, name=gid, kind="collection")
            group.add_members(members)
            groups.append(group)

    cobra_model.add_groups(groups)

    return cobra_model


# ----------------------
# Write SBML
# ----------------------
def write_sbml_model(cobra_model, filename, f_replace=F_REPLACE, **kwargs):
    """Writes cobra model to filename.

    The created model is SBML level 3 version 1 (L1V3) with
    fbc package v2 (fbc-v2).

    If the given filename ends with the suffix ".gz" (for example,
    "myfile.xml.gz"), libSBML assumes the caller wants the file to be
    written compressed in gzip format. Similarly, if the given filename
    ends with ".zip" or ".bz2", libSBML assumes the caller wants the
    file to be compressed in zip or bzip2 format (respectively). Files
    whose names lack these suffixes will be written uncompressed. Special
    considerations for the zip format: If the given filename ends with
    ".zip", the file placed in the zip archive will have the suffix
    ".xml" or ".sbml".  For example, the file in the zip archive will
    be named "test.xml" if the given filename is "test.xml.zip" or
    "test.zip". Similarly, the filename in the archive will be
    "test.sbml" if the given filename is "test.sbml.zip".

    Parameters
    ----------
    cobra_model : cobra.core.Model
        Model instance which is written to SBML
    filename : string
        path to which the model is written
    use_fbc_package : boolean {True, False}
        should the fbc package be used
    f_replace: dict of replacement functions for id replacement
    """
    doc = _model_to_sbml(cobra_model, f_replace=f_replace, **kwargs)

    if isinstance(filename, string_types):
        # write to path
        libsbml.writeSBMLToFile(doc, filename)

    elif hasattr(filename, "write"):
        # write to file handle
        sbml_str = libsbml.writeSBMLToString(doc)
        filename.write(sbml_str)


def _model_to_sbml(cobra_model, f_replace=None, units=True):
    """Convert Cobra model to SBMLDocument.

    Parameters
    ----------
    cobra_model : cobra.core.Model
        Cobra model instance
    f_replace : dict of replacement functions
        Replacement to apply on identifiers.
    units : boolean
        Should the FLUX_UNITS be written in the SBMLDocument.

    Returns
    -------
    libsbml.SBMLDocument
    """
    if f_replace is None:
        f_replace = {}

    sbmlns = libsbml.SBMLNamespaces(3, 1)  # SBML L3V1
    sbmlns.addPackageNamespace("fbc", 2)  # fbc-v2

    doc = libsbml.SBMLDocument(sbmlns)  # type: libsbml.SBMLDocument
    doc.setPackageRequired("fbc", False)
    doc.setSBOTerm(SBO_FBA_FRAMEWORK)
    model = doc.createModel()  # type: libsbml.Model
    model_fbc = model.getPlugin("fbc")  # type: libsbml.FbcModelPlugin
    model_fbc.setStrict(True)

    if cobra_model.id is not None:
        model.setId(cobra_model.id)
    if cobra_model.name is not None:
        model.setName(cobra_model.name)

    # Meta information (ModelHistory)
    if hasattr(cobra_model, "_sbml"):
        # print("WRITE", cobra_model._sbml["info"])
        meta = cobra_model._sbml
        history = libsbml.ModelHistory()  # type: libsbml.ModelHistory
        if "created" in meta:
            history.setCreatedDate(meta["created"])
        if "annotation" in meta:
            _sbase_annotations(doc, meta["annotation"])
            _sbase_notes_dict(doc, meta["notes"])
        if "creators" in meta:
            for creator in meta["creators"]:
                c = libsbml.ModelCreator()  # type: libsbml.ModelCreator
                if creator.get("familyName", None):
                    c.setFamilyName(creator["familyName"])
                if creator.get("givenName", None):
                    c.setFamilyName(creator["givenName"])
                if creator.get("organisation", None):
                    c.setFamilyName(creator["organisation"])
                if creator.get("email", None):
                    c.setFamilyName(creator["email"])
                history.addCreator(c)

    # Units
    if units:
        # type: libsbml.UnitDefinition
        flux_udef = model.createUnitDefinition()
        flux_udef.setId(UNITS_FLUX[0])
        for u in UNITS_FLUX[1]:
            unit = flux_udef.createUnit()  # type: libsbml.Unit
            unit.setKind(u.kind)
            unit.setExponent(u.exponent)
            unit.setScale(u.scale)
            unit.setMultiplier(u.multiplier)

    # minimum and maximum value from model
    if len(cobra_model.reactions) > 0:
        min_value = min(cobra_model.reactions.list_attr("lower_bound"))
        max_value = max(cobra_model.reactions.list_attr("upper_bound"))
    else:
        min_value = LOWER_BOUND
        max_value = UPPER_BOUND

    _create_parameter(model, pid=LOWER_BOUND_ID,
                      value=min_value, sbo=SBO_DEFAULT_FLUX_BOUND)
    _create_parameter(model, pid=UPPER_BOUND_ID,
                      value=max_value, sbo=SBO_DEFAULT_FLUX_BOUND)
    _create_parameter(model, pid=ZERO_BOUND_ID,
                      value=0, sbo=SBO_DEFAULT_FLUX_BOUND)

    # Compartments
    # FIXME: use first class compartment model (and write notes and annotations)
    #     (https://github.com/opencobra/cobrapy/issues/811)
    for cid, name in iteritems(cobra_model.compartments):
        c = model.createCompartment()  # type: libsbml.Compartment
        c.setId(cid)
        c.setName(name)
        c.setConstant(True)

        # FIXME: write annotations and notes
        # _sbase_notes(c, com.notes)
        # _sbase_annotations(c, com.annotation)

    # Species
    for met in cobra_model.metabolites:
        s = model.createSpecies()  # type: libsbml.Species
        mid = met.id
        if f_replace and F_SPECIE_REV in f_replace:
            mid = f_replace[F_SPECIE_REV](mid)
        s.setId(mid)
        s.setConstant(True)
        s.setBoundaryCondition(True)
        s.setHasOnlySubstanceUnits(False)
        s.setName(met.name)
        s.setCompartment(met.compartment)
        s_fbc = s.getPlugin("fbc")  # type: libsbml.FbcSpeciesPlugin
        if met.charge is not None:
            s_fbc.setCharge(met.charge)
        if met.formula is not None:
            s_fbc.setChemicalFormula(met.formula)

        _sbase_annotations(s, met.annotation)
        _sbase_notes_dict(s, met.notes)

    # Genes
    for gene in cobra_model.genes:
        gp = model_fbc.createGeneProduct()  # type: libsbml.GeneProduct
        gid = gene.id
        if f_replace and F_GENE_REV in f_replace:
            gid = f_replace[F_GENE_REV](gid)
        gp.setId(gid)
        gname = gene.name
        if gname is None or len(gname) == 0:
            gname = gid
        gp.setName(gname)
        gp.setLabel(gid)

        _sbase_annotations(gp, gene.annotation)
        _sbase_notes_dict(gp, gene.notes)

    # Objective
    objective = model_fbc.createObjective()  # type: libsbml.Objective
    objective.setId("obj")
    objective.setType(SHORT_LONG_DIRECTION[cobra_model.objective.direction])
    model_fbc.setActiveObjectiveId("obj")

    # Reactions
    reaction_coefficients = linear_reaction_coefficients(cobra_model)
    for reaction in cobra_model.reactions:
        rid = reaction.id
        if f_replace and F_REACTION_REV in f_replace:
            rid = f_replace[F_REACTION_REV](rid)
        r = model.createReaction()  # type: libsbml.Reaction
        r.setId(rid)
        r.setName(reaction.name)
        r.setFast(False)
        r.setReversible((reaction.lower_bound < 0))
        _sbase_annotations(r, reaction.annotation)
        _sbase_notes_dict(r, reaction.notes)

        # stoichiometry
        for metabolite, stoichiometry in iteritems(reaction._metabolites):
            sid = metabolite.id
            if f_replace and F_SPECIE_REV in f_replace:
                sid = f_replace[F_SPECIE_REV](sid)
            if stoichiometry < 0:
                sref = r.createReactant()  # type: libsbml.SpeciesReference
                sref.setSpecies(sid)
                sref.setStoichiometry(-stoichiometry)
                sref.setConstant(True)
            else:
                sref = r.createProduct()  # type: libsbml.SpeciesReference
                sref.setSpecies(sid)
                sref.setStoichiometry(stoichiometry)
                sref.setConstant(True)

        # bounds
        r_fbc = r.getPlugin("fbc")  # type: libsbml.FbcReactionPlugin
        r_fbc.setLowerFluxBound(_create_bound(model, reaction, "lower_bound",
                                              f_replace=f_replace, units=units,
                                              flux_udef=flux_udef))
        r_fbc.setUpperFluxBound(_create_bound(model, reaction, "upper_bound",
                                              f_replace=f_replace, units=units,
                                              flux_udef=flux_udef))

        # GPR
        gpr = reaction.gene_reaction_rule
        if gpr is not None and len(gpr) > 0:
            gpa = r_fbc.createGeneProductAssociation()  # noqa: E501 type: libsbml.GeneProductAssociation
            # replace ids
            if f_replace and F_GENE_REV in f_replace:
                tokens = gpr.split(' ')
                for k in range(len(tokens)):
                    if tokens[k] not in ['and', 'or', '(', ')']:
                        tokens[k] = f_replace[F_GENE_REV](tokens[k])
                gpr = " ".join(tokens)

            gpa.setAssociation(gpr)

        # objective coefficients
        if reaction_coefficients.get(reaction, 0) != 0:
            flux_obj = objective.createFluxObjective()  # noqa: E501 type: libsbml.FluxObjective
            flux_obj.setReaction(rid)
            flux_obj.setCoefficient(reaction.objective_coefficient)

    # write groups
    if len(cobra_model.groups) > 0:
        doc.enablePackage(
            "http://www.sbml.org/sbml/level3/version1/groups/version1",
            "groups", True)
        doc.setPackageRequired("groups", False)
        model_group = model.getPlugin("groups")  # noqa: E501 type: libsbml.GroupsModelPlugin
        for group in cobra_model.groups:
            g = model_group.createGroup()  # type: libsbml.Group
            g.setId(group.id)
            g.setName(group.name)
            g.setKind(group.kind)

            _sbase_notes_dict(g, group.notes)
            _sbase_annotations(g, group.annotation)

            for member in group.members:
                m = g.createMember()  # type: libsbml.Member
                mid = member.id
                m_type = str(type(member))

                # id replacements
                if "Reaction" in m_type:
                    if f_replace and F_REACTION_REV in f_replace:
                        mid = f_replace[F_REACTION_REV](mid)
                if "Metabolite" in m_type:
                    if f_replace and F_SPECIE_REV in f_replace:
                        mid = f_replace[F_SPECIE_REV](mid)
                if "Gene" in m_type:
                    if f_replace and F_GENE_REV in f_replace:
                        mid = f_replace[F_GENE_REV](mid)

                m.setIdRef(mid)
                if member.name and len(member.name) > 0:
                    m.setName(member.name)

    return doc


def _create_bound(model, reaction, bound_type, f_replace, units=None,
                  flux_udef=None):
    """Creates bound in model for given reaction.

    Adds the parameters for the bounds to the SBML model.

    Parameters
    ----------
    model : libsbml.Model
        SBML model instance
    reaction : cobra.core.Reaction
        Cobra reaction instance from which the bounds are read.
    bound_type : {LOWER_BOUND, UPPER_BOUND}
        Type of bound
    f_replace : dict of id replacement functions
    units : flux units

    Returns
    -------
    Id of bound parameter.
    """
    value = getattr(reaction, bound_type)
    if value == LOWER_BOUND:
        return LOWER_BOUND_ID
    elif value == 0:
        return ZERO_BOUND_ID
    elif value == UPPER_BOUND:
        return UPPER_BOUND_ID
    else:
        # new parameter
        rid = reaction.id
        if f_replace and F_REACTION_REV in f_replace:
            rid = f_replace[F_REACTION_REV](rid)
        pid = rid + "_" + bound_type
        _create_parameter(model, pid=pid, value=value, sbo=SBO_FLUX_BOUND,
                          units=units, flux_udef=flux_udef)
        return pid


def _create_parameter(model, pid, value, sbo=None, constant=True, units=None,
                      flux_udef=None):
    """Create parameter in SBML model."""
    p = model.createParameter()  # type: libsbml.Parameter
    p.setId(pid)
    p.setValue(value)
    p.setConstant(constant)
    if sbo:
        p.setSBOTerm(sbo)
    if units:
        p.setUnits(flux_udef.getId())


def _check_required(sbase, value, attribute):
    """Get required attribute from the SBase.

    Parameters
    ----------
    sbase : libsbml.SBase
    value : existing value
    attribute: name of attribute

    Returns
    -------
    attribute value (or value if already set)
    """
    if value is None:
        msg = "required attribute '%s' not found in '%s'" % \
              (attribute, sbase)
        if sbase.getId() is not None:
            msg += " with id '%s'" % sbase.getId()
        elif sbase.getName() is not None:
            msg += " with name '%s'" % sbase.getName()
        raise CobraSBMLError(msg)
    return value


def _check(value, message):
    """
    Checks the libsbml return value and prints message if something happened.

    If 'value' is None, prints an error message constructed using
      'message' and then exits with status code 1. If 'value' is an integer,
      it assumes it is a libSBML return status code. If the code value is
      LIBSBML_OPERATION_SUCCESS, returns without further action; if it is not,
      prints an error message constructed using 'message' along with text from
      libSBML explaining the meaning of the code, and exits with status code 1.

    """
    if value is None:
        LOGGER.error('Error: LibSBML returned a null value trying '
                     'to <' + message + '>.')
    elif type(value) is int:
        if value == libsbml.LIBSBML_OPERATION_SUCCESS:
            return
        else:
            LOGGER.error('Error encountered trying to <' + message + '>.')
            LOGGER.error('LibSBML returned error code {}: {}'.format(str(value),
                         libsbml.OperationReturnValue_toString(value).strip()))
    else:
        return


# ----------------------
# Notes
# ----------------------
# def _parse_notes(sbase):
#    return sbase.getNotesString()


def _parse_notes_dict(sbase):
    """ Creates dictionary of COBRA notes.

    Parameters
    ----------
    sbase : libsbml.SBase

    Returns
    -------
    dict of notes
    """
    notes = sbase.getNotesString()
    if notes and len(notes) > 0:
        pattern = r"<p>\s*(\w+\s*\w*)\s*:\s*([\w|\s]+)<"
        matches = re.findall(pattern, notes)
        d = {k.strip(): v.strip() for (k, v) in matches}
        return {k: v for k, v in d.items() if len(v) > 0}
    else:
        return {}


def _sbase_notes_dict(sbase, notes):
    """Set SBase notes based on dictionary.

    Parameters
    ----------
    sbase : libsbml.SBase
        SBML object to set notes on
    notes : notes object
        notes information from cobra object
    """
    if notes and len(notes) > 0:
        tokens = ['<html xmlns = "http://www.w3.org/1999/xhtml" >'] + \
            ["<p>{}: {}</p>".format(k, v) for (k, v) in notes.items()] + \
            ["</html>"]
        _check(
            sbase.setNotes("\n".join(tokens)),
            "Setting notes on sbase: {}".format(sbase)
        )


# ----------------------
# Annotations
# ----------------------
"""
cobra annotations will be dictionaries of the form:
    object.annotation = {
        'provider' : [(qualifier, entity), ...]
    }
A concrete example for a metabolite would look like the following
    metabolite.annotation = {
        'chebi': [(isVersionOf, "CHEBI:17234), (is, "CHEBI:4167),],
        'kegg.compound': [(is, "C00031")]
    }
The providers are hereby MIRIAM registry keys for collections
https://www.ebi.ac.uk/miriam/main/collections
The qualifiers are biomodel qualifiers
https://co.mbine.org/standards/qualifiers
"""

URL_IDENTIFIERS_PATTERN = r"^http[s]{0,1}://identifiers.org/(.+)/(.+)"
URL_IDENTIFIERS_PREFIX = r"https://identifiers.org"
QUALIFIER_TYPES = {
     "is": libsbml.BQB_IS,
     "hasPart": libsbml.BQB_HAS_PART,
     "isPartOf": libsbml.BQB_IS_PART_OF,
     "isVersionOf": libsbml.BQB_IS_VERSION_OF,
     "hasVersion": libsbml.BQB_HAS_VERSION,
     "isHomologTo": libsbml.BQB_IS_HOMOLOG_TO,
     "isDescribedBy": libsbml.BQB_IS_DESCRIBED_BY,
     "isEncodedBy": libsbml.BQB_IS_ENCODED_BY,
     "encodes": libsbml.BQB_ENCODES,
     "occursIn": libsbml.BQB_OCCURS_IN,
     "hasProperty": libsbml.BQB_HAS_PROPERTY,
     "isPropertyOf": libsbml.BQB_IS_PROPERTY_OF,
     "hasTaxon": libsbml.BQB_HAS_TAXON,
     "unknown": libsbml.BQB_UNKNOWN,
     "bqm_is": libsbml.BQM_IS,
     "bqm_isDescribedBy": libsbml.BQM_IS_DESCRIBED_BY,
     "bqm_isDerivedFrom": libsbml.BQM_IS_DERIVED_FROM,
     "bqm_isInstanceOf": libsbml.BQM_IS_INSTANCE_OF,
     "bqm_hasInstance": libsbml.BQM_HAS_INSTANCE,
     "bqm_unknown": libsbml.BQM_UNKNOWN,
}


def _parse_annotations(sbase):
    """Parses cobra annotations from a given SBase object.

    Annotations are dictionaries with the providers as keys.

    Parameters
    ----------
    sbase : libsbml.SBase
        SBase from which the SBML annotations are read

    Returns
    -------
    dict (annotation dictionary)

    FIXME: annotation format must be updated
        (https://github.com/opencobra/cobrapy/issues/684)
    """
    annotation = {}

    # SBO term
    if sbase.isSetSBOTerm():
        # FIXME: correct handling of annotations
        annotation["sbo"] = sbase.getSBOTermID()

    # RDF annotation
    cvterms = sbase.getCVTerms()
    if cvterms is None:
        return annotation

    for cvterm in cvterms:  # type: libsbml.CVTerm
        for k in range(cvterm.getNumResources()):
            uri = cvterm.getResourceURI(k)

            # FIXME: read and store the qualifier
            tokens = uri.split('/')
            if len(tokens) != 5 or not tokens[2] == "identifiers.org":
                LOGGER.warning("%s does not conform to "
                               "http(s)://identifiers.org/collection/id" % uri)
                continue

            provider, identifier = tokens[3], tokens[4]
            if provider in annotation:
                if isinstance(annotation[provider], string_types):
                    annotation[provider] = [annotation[provider]]
                annotation[provider].append(identifier)
            else:
                # FIXME: always in list
                annotation[provider] = identifier

    return annotation


def _sbase_annotations(sbase, annotation):
    """Set SBase annotations based on cobra annotations.

    Parameters
    ----------
    sbase : libsbml.SBase
        SBML object to annotate
    annotation : cobra annotation structure
        cobra object with annotation information

    FIXME: annotation format must be updated
        (https://github.com/opencobra/cobrapy/issues/684)
    """

    if not annotation or len(annotation) == 0:
        return

    # standardize annotations
    annotation_data = deepcopy(annotation)
    for key, v in annotation_data.items():
        if isinstance(v, string_types):
            annotation_data[key] = [("is", v)]

    for key, v in annotation_data.items():
        for idx, item in enumerate(v):
            if isinstance(item, string_types):
                v[idx] = ("is", item)

    # set metaId
    meta_id = "meta_{}".format(sbase.getId())
    sbase.setMetaId(meta_id)

    # rdf_items = []
    for provider, data in iteritems(annotation_data):

        # set SBOTerm
        if provider in ["SBO", "sbo"]:
            if provider == "SBO":
                logging.warning("'SBO' provider is deprecated, "
                                "use 'sbo' provider instead")
            sbo_term = data[0][1]
            _check(sbase.setSBOTerm(sbo_term),
                   "Setting SBOTerm: {}".format(sbo_term))

            # FIXME: sbo should also be written as CVTerm
            continue

        for item in data:
            qualifier_str, entity = item[0], item[1]
            qualifier = QUALIFIER_TYPES.get(qualifier_str, None)
            if qualifier is None:
                qualifier = libsbml.BQB_IS
                LOGGER.warning("Qualifier type is not supported on "
                               "annotation: '{}'".format(qualifier_str))

            qualifier_type = libsbml.BIOLOGICAL_QUALIFIER
            if qualifier_str.startswith("bqm_"):
                qualifier_type = libsbml.MODEL_QUALIFIER

            cv = libsbml.CVTerm()  # type: libsbml.CVTerm
            cv.setQualifierType(qualifier_type)
            if qualifier_type == libsbml.BIOLOGICAL_QUALIFIER:
                cv.setBiologicalQualifierType(qualifier)
            elif qualifier_type == libsbml.MODEL_QUALIFIER:
                cv.setModelQualifierType(qualifier)
            else:
                raise CobraSBMLError('Unsupported qualifier: '
                                     '%s' % qualifier)
            resource = "%s/%s/%s" % (URL_IDENTIFIERS_PREFIX, provider, entity)
            cv.addResource(resource)
            _check(sbase.addCVTerm(cv),
                   "Setting cvterm: {}, resource: {}".format(cv, resource))


# -----------------------------------
# Validation
# -----------------------------------
def validate_sbml_model(filename, use_libsbml=False, check_model=True,
                        internal_consistency=True,
                        check_units_consistency=False,
                        check_modeling_practice=False):
    """Validate SBML model and returns the model along with a list of errors.

    Parameters
    ----------
    filename : str
        The filename (or SBML string) of the SBML model to be validated.
    use_libsbml : boolean {True, False}
        Perform SBML validation via libsbml. This can take some time.
    internal_consistency: boolean {True, False}
        Check internal consistency.
    check_units_consistency: boolean {True, False}
        Check consistency of units.
    check_modeling_practice: boolean {True, False}
        Check modeling practise.
    check_model: boolean {True, False}
        Whether to also check some basic model properties such as reaction
        boundaries and compartment formulas.

    Returns
    -------
    model : :class:`~cobra.core.Model.Model` object
        The cobra model if the file could be read successfully or None
        otherwise.
    errors : dict
        Warnings and errors grouped by their respective types.

    Raises
    ------
    CobraSBMLError
        If the file is not a valid SBML Level 3 file with FBC.
    """
    # store errors
    errors = {key: [] for key in ("validator", "warnings", "other",
                                  "SBML errors")}
    if use_libsbml:
        for key in ["SBML_FATAL", "SBML ERROR", "SBML_SCHEMA_ERROR",
                    "SBML_WARNING"]:
            errors[key] = []

    def err(err_msg, group="validator"):
        errors[group].append(err_msg)

    # make sure there is exactly one model
    doc = _get_doc_from_filename(filename)
    model = doc.getModel()  # type: libsbml.Model
    if model is None:
        raise CobraSBMLError("No SBML model detected in file.")

    if use_libsbml:
        # set the unit checking, similar for the other settings
        doc.setConsistencyChecks(libsbml.LIBSBML_CAT_UNITS_CONSISTENCY,
                                 check_units_consistency)
        doc.setConsistencyChecks(libsbml.LIBSBML_CAT_MODELING_PRACTICE,
                                 check_modeling_practice)

        # validate the document
        if internal_consistency:
            doc.checkInternalConsistency()
        doc.checkConsistency()

        for k in range(doc.getNumErrors()):
            e = doc.getError(k)
            sev = e.getSeverity()
            if sev == libsbml.LIBSBML_SEV_FATAL:
                err(_error_string(e), "SBML_FATAL")
            elif sev == libsbml.LIBSBML_SEV_ERROR:
                err(_error_string(e), "SBML_ERROR")
            elif sev == libsbml.LIBSBML_SEV_SCHEMA_ERROR:
                err(_error_string(e), "SBML_SCHEMA_ERROR")
            elif sev == libsbml.LIBSBML_SEV_WARNING:
                err(_error_string(e), "SBML_WARNING")

    # ensure can be made into model
    # all warnings generated while loading will be logged as errors
    with catch_warnings(record=True) as warning_list:
        simplefilter("always")
        try:
            model = _sbml_to_model(doc)
        except CobraSBMLError as e:
            err(str(e), "SBML errors")
            return None, errors
        except Exception as e:
            err(str(e), "other")
            return None, errors
    errors["warnings"].extend(str(i.message) for i in warning_list)

    if check_model:
        errors["validator"].extend(check_metabolite_compartment_formula(model))

    return model, errors


def _error_string(error, k=None):
    """String representation of SBMLError.

    Parameters
    ----------
    error : libsbml.SBMLError
    k : index of error

    Returns
    -------
    string representation of error
    """
    package = error.getPackage()
    if package == '':
        package = 'core'

    error_str = 'E{}: {} ({}, L{}, {})  \n' \
                '{}\n' \
                '[{}] {}\n' \
                '{}\n'.format(
                    k, error.getCategoryAsString(), package, error.getLine(),
                    'code',
                    '-' * 60,
                    error.getSeverityAsString(), error.getShortMessage(),
                    error.getMessage())
    return error_str
