"""Utility functions for working with xarray Datasets.
"""
import collections
import functools
import itertools
import re
import warnings

import cftime # believe explict import needed for cf_xarray date parsing?
import cf_xarray
import xarray as xr

from src import util, units, core

import logging
_log = logging.getLogger(__name__)

# TODO: put together a proper CV for all CF convention attributes
_cf_calendars = (
    "gregorian",
    "standard", # synonym for gregorian
    "proleptic_gregorian",
    "julian",
    "noleap",
    "365_day", # synonym for noleap
    "all_leap",
    "366_day", # synonym for all_leap
    "360_day",
    "none"
)

ATTR_NOT_FOUND = util.sentinel_object_factory('AttrNotFound')
ATTR_NOT_FOUND.__doc__ = """
Sentinel object serving as a placeholder for netCDF metadata attributes that 
are expected, but not present in the data. 
"""

@util.mdtf_dataclass
class PlaceholderScalarCoordinate():
    """Dummy object used to describe scalar coordinates referred to by name only
    in the 'coordinates' attribute of a variable or dataset. We do this so that
    the attributes match those of coordinates represented by real netCDF Variables.
    """
    name: str
    axis: str
    standard_name: str = ATTR_NOT_FOUND
    units: str = ATTR_NOT_FOUND

# ========================================================================
# Customize behavior of cf_xarray accessor 
# (https://github.com/xarray-contrib/cf-xarray, https://cf-xarray.readthedocs.io/en/latest/)

def patch_cf_xarray_accessor(mod):
    """Monkey-patches ``_get_axis_coord``, a module-level function in cf_xarray,
    to obtain desired behavior.
    """
    _ax_to_coord = {
        "X": ("longitude", ),
        "Y": ("latitude", ),
        "Z": ("vertical", ),
        "T": ("time", )
    }
    func_name = "_get_axis_coord"
    old_get_axis_coord = getattr(mod, func_name, None)
    assert old_get_axis_coord is not None
    
    @functools.wraps(old_get_axis_coord)
    def new_get_axis_coord(var, key):
        """Modify cf_xarray behavior: If a variable has been recognized as one of 
        the coordinates in the dict above **and** no variable has been set as the
        corresponding axis, recognize the variable as that axis as well.
        See discussion at `https://github.com/xarray-contrib/cf-xarray/issues/23`__.
        
        Args:
            var: Dataset or DataArray to be queried
            key: axis or coordinate name.
            
        Returns list of variable names in var matching key.
        """
        var_names = old_get_axis_coord(var, key)
        if var_names or (key not in _ax_to_coord):
            # unchanged behavior:
            return var_names
        # remaining case: key is an axis name and no var_names
        for new_key in _ax_to_coord[key]:
            var_names.extend(old_get_axis_coord(var, new_key))
        return var_names
    
    setattr(mod, func_name, new_get_axis_coord)
    
patch_cf_xarray_accessor(cf_xarray.accessor)

class MDTFCFAccessorMixin(object):
    """Methods we add for both xarray Dataset and DataArray objects, although 
    intended use case will be to call them once per Dataset.
    """
    @property
    def is_static(self):
        return bool(cf_xarray.accessor._get_axis_coord(self._obj, "T"))

    @property
    def calendar(self):
        """Reads 'calendar' attribute on time axis (intended to have been set
        by set_calendar()). Returns None if no time axis.
        """
        ds = self._obj # abbreviate
        t_names = cf_xarray.accessor._get_axis_coord(ds, "T")
        if not t_names:
            return None
        assert len(t_names) == 1
        return ds.coords[t_names[0]].attrs.get('calendar', None)

    def _old_axes_dict(self, var_name=None):
        """Code for the "axes" accessor behavior as defined in `cf\_xarray 
        <https://cf-xarray.readthedocs.io/en/latest/generated/xarray.DataArray.cf.axes.html#xarray.DataArray.cf.axes>`__,
        which we override in various ways below. 

        Args:
            var_name (optional): If supplied, return a dict containing the subset
                of coordinates used by the dependent variable *var\_name*, instead
                of all coordinates in the dataset. 

        Returns:
            dict mapping axes labels to lists of names of variables in the 
            DataSet that the accessor has mapped to that axis.
        """
        if var_name is None:
            axes_obj = self._obj
        else:
            # filter Dataset on axes associated with a specific variable
            assert isinstance(self._obj, xr.core.dataset.Dataset)
            axes_obj = self._obj[var_name]
        vardict = {
            key: cf_xarray.accessor.apply_mapper(
                cf_xarray.accessor._get_axis_coord, axes_obj, key, error=False
            ) for key in cf_xarray.accessor._AXIS_NAMES
        }
        if var_name is None:
            return {k: sorted(v) for k, v in vardict.items() if v}
        # case where var_name given:    
        # do validation on cf_xarray.accessor's work, since it turns out it
        # can get confused on real-world data
        empty_keys = []
        delete_keys = []
        dims_list = list(axes_obj.dims)
        for k,v in vardict.items():
            if len(v) > 1 and var_name is not None: 
                _log.error('Too many %s axes found for %s: %s', k, var_name, v)
                raise TypeError(f"Too many {k} axes for {var_name}.")
            elif len(v) == 1:
                if v[0] not in dims_list:
                    _log.warning(("cf_xarray fix: %s axis %s not in dimensions "
                        "for %s; dropping."), k, v[0], var_name)
                    delete_keys.append(k)
                else:
                    dims_list.remove(v[0])
            else:
                empty_keys.append(k)
        if len(dims_list) > 0:
            # didn't assign all dims for this var
            if len(dims_list) == 1 and len(empty_keys) == 1:
                _log.warning('cf_xarray fix: assuming %s is %s axis for %s',
                    dims_list[0], empty_keys[0], var_name)
                vardict[empty_keys[0]] = [dims_list[0]]
            else:
                _log.error(("cf_xarray error: couldn't assign %s to axes for %s"
                    "(assigned axes: %s)"), dims_list, var_name, vardict)
                raise TypeError(f"Missing axes for {var_name}.")
        for k in delete_keys:
            vardict[k] = []
        return {k: sorted(v) for k, v in vardict.items() if v}

    @property
    def dim_axes_set(self):
        return frozenset(self._obj.cf.dim_axes().keys())

    @property
    def axes_set(self):
        return frozenset(self._obj.cf.axes().keys())

class MDTFCFDatasetAccessorMixin(MDTFCFAccessorMixin):
    """Methods we add for xarray Dataset objects.
    """
    def scalar_coords(self, var_name=None):
        """Return a list of the Dataset variables corresponding to scalar coordinates.
        If coordinate was defined as an attribute only, store its name instead.
        """
        ds = self._obj
        axes_d = ds.cf._old_axes_dict(var_name=var_name)
        scalars = []
        for ax, coord_names in axes_d.items():
            for c in coord_names:
                if c in ds:
                    if (c not in ds.dims or (ds[c].size == 1 and ax == 'Z')):
                        scalars.append(ds[c])
                else:
                    if c not in ds.dims:
                        # scalar coord set from Dataset attribute, so we only 
                        # have name and axis
                        dummy_coord = PlaceholderScalarCoordinate(name=c, axis=ax)
                        scalars.append(dummy_coord)
        return scalars

    def get_scalar(self, ax_name, var_name=None):
        """If the axis label *ax_name* is a scalar coordinate, return the 
        corresponding xarray DataArray (or PlaceholderScalarCoordinate), otherwise 
        return None.
        """
        for c in self.scalar_coords(var_name=var_name):
            if c.axis == ax_name:
                return c
        return None

    def axes(self, var_name=None, filter_set=None):
        """Override cf_xarray accessor behavior 
        (from :meth:`~MDTFCFAccessorMixin._old_axes_dict).

        Args:
            var_name (optional): If supplied, return a dict containing the subset
                of coordinates used by the dependent variable *var\_name*, instead
                of all coordinates in the dataset. 
            filter_set (optional): Optional iterable of coordinate names. If 
                supplied, restrict the returned dict to coordinates in *filter\_set*.

        Returns:
            dict mapping axis labels to lists of the Dataset variables themselves, 
            instead of their names.
        """
        ds = self._obj
        axes_d = ds.cf._old_axes_dict(var_name=var_name)
        d = dict()
        for ax, coord_names in axes_d.items():
            new_coords = []
            for c in coord_names:
                if not c or (filter_set is not None and c not in filter_set):
                    continue
                if c in ds:
                    new_coords.append(ds[c])
                else:
                    # scalar coord set from Dataset attribute, so we only 
                    # have name and axis
                    if var_name is not None:
                        assert c not in ds[var_name].dims
                    dummy_coord = PlaceholderScalarCoordinate(name=c, axis=ax)
                    new_coords.append(dummy_coord)
            if new_coords:
                if var_name is not None:
                    # Verify that we only have one coordinate for each axis if
                    # we're getting axes for a single variable
                    if len(new_coords) != 1:
                        raise TypeError(f"More than one {ax} axis found for "
                            f"'{var_name}': {new_coords}.")
                    d[ax] = new_coords[0]
                else:
                    d[ax] = new_coords
        return d

    def dim_axes(self, var_name=None):
        """Override cf_xarray accessor behavior by having values of the 'axes'
        dict be the Dataset variables themselves, instead of their names.
        """
        return self.axes(var_name=var_name, filter_set=self._obj.dims)

class MDTFDataArrayAccessorMixin(MDTFCFAccessorMixin):
    """Methods we add for xarray DataArray objects.
    """
    def dim_axes(self):
        """Map axes labels to the (unique) coordinate variable name,
        instead of a list of names as in cf_xarray. Filter on dimension coordinates
        only (eliminating any scalar coordinates.)
        """
        return {k:v for k,v in self._obj.cf.axes().items() if v in self._obj.dims}

    def axes(self):
        """Map axes labels to the (unique) coordinate variable name,
        instead of a list of names as in cf_xarray.
        """
        d = self._obj.cf._old_axes_dict()
        return {k: v[0] for k,v in d.items()}

    @property
    def formula_terms(self):
        """Returns dict of (name in formula: name in dataset) pairs parsed from
        formula_terms attribute. If attribute not present, returns empty dict.
        """
        terms = dict()
        # NOTE: more permissive than munging used in cf_xarray
        formula_terms = self._obj.attrs.get('formula_terms', '')
        for mapping in re.sub(r"\s*:\s*", ":", formula_terms).split():
            key, value = mapping.split(":")
            terms[key] = value
        return terms
    
with warnings.catch_warnings():
    # cf_xarray registered its accessors under "cf". Re-registering our versions
    # will work correctly, but raises the following warning, which we suppress.
    warnings.simplefilter(
        'ignore', category=xr.core.extensions.AccessorRegistrationWarning
    )

    @xr.register_dataset_accessor("cf")
    class MDTFCFDatasetAccessor(
        MDTFCFDatasetAccessorMixin, cf_xarray.accessor.CFDatasetAccessor
    ):
        pass
    
    @xr.register_dataarray_accessor("cf")
    class MDTFCFDataArrayAccessor(
        MDTFDataArrayAccessorMixin, cf_xarray.accessor.CFDataArrayAccessor
    ):
        pass

# ========================================================================

class DatasetParser():
    """Class which acts as a container for MDTF-specific dataset parsing logic.
    """
    def __init__(self):
        config = dict() # core.ConfigManager()
        self.skip_std_name = config.get('disable_CF_name_checks', False)
        self.skip_units = config.get('disable_unit_checks', False)

        self.fallback_cal = 'proleptic_gregorian' # calendar used if no attribute found
        self.attrs_backup = dict()
        self.log = _log # temporary

    # --- Methods for initial munging, prior to xarray.decode_cf -------------

    def guess_attr(self, attr_desc, attr_name, options, default=None, 
        comparison_func=None):
        """Select and return element of *options* equal to *attr_name*. 
        If none are equal, try a case-insensititve string match.

        Args:
            attr_desc (str): Description of the attribute (only used for log
                messages.)
            attr_name (str): Expected name of the attribute.
            options (iterable of str): Attribute names that are present in the 
                data.
            default (str, default None): If supplied, default value to return if
                no match.
            comparison_func (optional, default None): String comparison function
                to use.

        Raises:
            KeyError if no element of *options* can be coerced to match *key_name*.
        """
        def str_munge(s):
            # for case-insensitive comparison function: make string lowercase
            # and drop non-alphanumeric chars
            return re.sub(r'[^a-z0-9]+', '', s.lower())

        if comparison_func is None:
            comparison_func = (lambda x,y: x == y)
        options = util.to_iter(options)
        test_count = sum(comparison_func(opt, attr_name) for opt in options)
        if test_count > 1:
            self.log.debug("Found multiple values of '%s' set for '%s'.", 
                attr_name, attr_desc)
        if test_count >= 1:
            return attr_name
        munged_opts = [
            (comparison_func(str_munge(opt), str_munge(attr_name)), opt) \
                for opt in options
        ]
        if sum(tup[0] for tup in munged_opts) == 1:
            guessed_attr = [tup[1] for tup in munged_opts if tup[0]][0]
            self.log.debug("Correcting '%s' to '%s' as the intended value for '%s'.", 
                attr_name, guessed_attr, attr_desc,
                tags=util.ObjectLogTag.NC_HISTORY)
            return guessed_attr
        # Couldn't find a match
        if default is not None:
            return default
        raise KeyError(attr_name)

    def normalize_attr(self, new_attr_d, d, key_name, key_startswith=None):
        """Sets the value in dict *d* corresponding to the key *key_name*. 
        
        If *key_name* is in *d*, no changes are made. If *key_name* is not in 
        *d*, we check possible nonstandard representations of the key 
        (case-insensitive match via :meth:`guess_attr` and whether the key 
        starts with the string *key_startswith*.) If no match is found for
        *key_name*, its value is set to the sentinel value ATTR_NOT_FOUND.

        Args:
            new_attr_d (dict): dict to store all found attributes. We don't 
                change attributes on *d* here, since that can interfere with
                xarray.decode_cf(), but instead pass this to :meth:`restore_attrs`
                so they can be set once that's done.
            d (dict): dict of DataSet attributes, whose keys are to be searched 
                for *key_name*.
            key_name (str): Expected name of the key.
            key_startswith (optional, str): If provided and if *key_name* isn't 
                found in *d*, a key starting with this string will be accepted 
                instead.
        """
        try:
            k = self.guess_attr(
                key_name, key_name, d.keys(), 
                comparison_func=None
            )
        except KeyError:
            if key_startswith is None:
                k = None
            else:
                try:
                    k = self.guess_attr(
                        key_name, key_startswith, d.keys(),
                        comparison_func=(lambda x,y: x.startswith(y))
                    )
                except KeyError:
                    k = None
        if k is None:
            # key wasn't found
            new_attr_d[key_name] = ATTR_NOT_FOUND
        elif k != key_name:
            # key found with a different name than expected
            new_attr_d[key_name] = d.get(k, ATTR_NOT_FOUND)
        else:
            # key was found with expected name; copy to new_attr_d
            new_attr_d[key_name] = d[key_name]

    def normalize_standard_name(self, new_attr_d, attr_d):
        """Method for munging standard_name attribute prior to parsing.
        """
        self.normalize_attr(new_attr_d, attr_d, 'standard_name', 'standard')

    def normalize_unit(self, new_attr_d, attr_d):
        """HACK to convert unit strings to values that are correctly parsed by
        cfunits/UDUnits2. Currently we handle the case where "mb" is interpreted
        as "millibarn", a unit of area (see UDUnits `mailing list 
        <https://www.unidata.ucar.edu/support/help/MailArchives/udunits/msg00721.html>`__.)
        """
        self.normalize_attr(new_attr_d, attr_d, 'units', 'unit')
        unit_str = new_attr_d['units']
        if unit_str is not ATTR_NOT_FOUND:
            # regex matches "mb", case-insensitive, provided the preceding and 
            # following characters aren't also letters; expression replaces 
            # "mb" with "millibar", which is interpreted correctly.
            unit_str = re.sub(
                r"(?<![^a-zA-Z])([mM][bB])(?![^a-zA-Z])", "millibar", unit_str
            )
            # TODO: insert other cases of misidentified units here as they're 
            # discovered
            new_attr_d['units'] = unit_str

    def normalize_calendar(self, attr_d):
        """Finds the calendar attribute, if present, and normalizes it to one of
        the values in the CF standard before xarray.decode_cf decodes the 
        time axis.
        """
        self.normalize_attr(attr_d, attr_d, 'calendar', 'cal')
        if attr_d['calendar'] == ATTR_NOT_FOUND:
            # in normal operation, this is because we're looking at a non-time-
            # related variable. Calendar assignment is finalized by check_calendar().
            del attr_d['calendar']
        else:
            # Make sure it's set to a recognized value
            attr_d['calendar'] = self.guess_attr(
                'calendar', attr_d['calendar'], _cf_calendars, 
                default=self.fallback_cal
            )

    def normalize_ds_attrs(self, ds):
        """Initial munging of xarray Dataset attribute dicts, before any 
        parsing by xarray.decode_cf() or the cf_xarray accessor.
        """
        def strip_(v):
            # strip leading, trailing whitespace from all string-valued attributes
            return (v.strip() if isinstance(v, str) else v)
        def strip_attrs(obj):
            d = getattr(obj, 'attrs', dict())
            return {strip_(k): strip_(v) for k,v in d.items()}

        setattr(ds, 'attrs', strip_attrs(ds))
        for var in ds.variables:
            new_d = dict()
            d = strip_attrs(ds[var])
            setattr(ds[var], 'attrs', d) 
            # need to do this hacky new_d/d stuff because if we updated attrs on d
            # now, we'd hit an exception from xr.decode_cf() ("TypeError: argument 
            # of type '_SentinelObject' is not iterable"). Instead we apply the
            # edits to new_d, save that in attrs_backup, and reapply later in 
            # restore_attrs_backup().
            self.normalize_standard_name(new_d, d)
            self.normalize_unit(new_d, d)
            self.normalize_calendar(d)
            self.attrs_backup[var] = d.copy()
            self.attrs_backup[var].update(new_d)
        self.attrs_backup['Dataset'] = ds.attrs.copy()

    def restore_attrs_backup(self, ds):
        """xarray.decode_cf() and other functions appear to un-set some of the 
        attributes defined in the netCDF file. Restore them from the backups 
        made in :meth:`munge_ds_attrs`, but only if the attribute was deleted.
        """
        def _restore_one(name, attrs_d):
            backup_d = self.attrs_backup.get(name, dict())
            for k,v in backup_d.items():
                if k not in attrs_d:
                    attrs_d[k] = v
                if v != attrs_d[k] and v != ATTR_NOT_FOUND:
                    self.log.error("%s: discrepancy for attr '%s': '%s' != '%s'.",
                        name, k, v, attrs_d[k])
        
        _restore_one('Dataset', ds.attrs)
        for var in ds.variables:
            _restore_one(var, ds[var].attrs)


    # --- Methods for comparing attrs against our record (TranslatedVarlistEntry) ---

    def compare_attr(self, our_attr_tuple, ds_attr_tuple, comparison_func=None, 
        fill_ours=True, fill_ds=False, overwrite_ours=None):
        """Worker function to compare two attributes (on *our_var*, the 
        framework's record, and on *ds*, the "ground truth" of the dataset) and 
        update one in the event of disagreement.

        This handles the special cases where the attribute isn't defined on 
        *our_var* or *ds*.

        Args:
            our_attr_tuple: tuple specifying the attribute on *our_var*
            ds_attr_tuple: tuple specifying the same attribute on *ds*
            comparison_func: function of two arguments to use to compare the 
                attributes; defaults to ``__eq__``.
            fill_ours (bool): If the attr on *our_var* is missing, fill it in
                with the value from *ds*.
            fill_ds (bool): If the attr on *ds* is missing, fill it in
                with the value from *our_var*.
            overwrite_ours (bool): Action to take if both attrs are defined but 
                have different values:

                - None (default): Update *our_var* if *fill_ours* is True,
                    but in any case raise a :class:`~util.MetadataEvent`.
                - True: Change *our_var* to match *ds*.
                - False: Change *ds* to match *our_var*.
        """
        # unpack tuples
        our_var, our_attr_name, our_attr = our_attr_tuple
        ds_var, ds_attr_name, ds_attr = ds_attr_tuple
        if comparison_func is None:
            comparison_func = (lambda x,y: x == y)
        can_fill_ours = (fill_ours and ds_attr != ATTR_NOT_FOUND) # abbreviate

        if ds_attr == ATTR_NOT_FOUND or (not ds_attr):
            # ds_attr wasn't defined
            if fill_ds:
                # update ds with our value
                self.log.warning("No %s for '%s' found in dataset; setting to '%s'.",
                    ds_attr_name, ds_var.name, str(our_attr))
                ds_var.attrs[ds_attr_name] = str(our_attr)
                return
            else:
                # don't change ds, raise exception
                raise util.MetadataError((f"No {ds_attr_name} for '{ds_var.name}' "
                    f"(= {our_attr}) found in dataset."))

        if not our_attr:
            # our_attr wasn't defined
            if can_fill_ours:
                if not (our_attr_name == 'name' and our_var.name == ds_attr):
                    self.log.debug("Updating %s for '%s' to value '%s' from dataset.",
                        our_attr_name, our_var.name, ds_attr)
                setattr(our_var, our_attr_name, ds_attr)
                return
            else:
                # don't change ds, raise exception
                raise util.MetadataError((f"'{our_var.name}' not set but "
                    f"{ds_attr_name} (= {ds_attr}) present in dataset."))

        if not comparison_func(our_attr, ds_attr):
            # both attrs present, but have different values
            if overwrite_ours is None:
                if can_fill_ours:
                    # update our attr with value from ds, but also raise error
                    setattr(our_var, our_attr_name, ds_attr)
                raise util.MetadataEvent((f"Unexpected {our_attr_name} for variable "
                    f"'{our_var.name}': '{ds_attr}' (expected '{our_attr}')."))
            elif overwrite_ours:
                # set our attr to ds value
                self.log.debug("Updating %s for '%s' to value '%s' from dataset.",
                    our_attr_name, our_var.name, ds_attr)
                setattr(our_var, our_attr_name, ds_attr)
                return
            else:
                # set ds attr to our value
                self.log.info(("Changing %s for '%s' in dataset from '%s' to our "
                    "value '%s'."),
                    ds_attr_name, our_var.name, ds_attr, our_attr)
                ds_var.attrs[ds_attr_name] = str(our_attr)
                return
        else:
            # comparison passed, no changes needed
            return

    def reconcile_name(self, our_var, ds_var_name, overwrite_ours=None):
        """Reconcile the name of the variable between the 'ground truth' of the 
        dataset we downloaded (*ds_var*) and our expectations based on the model's
        convention (*our_var*).
        """
        attr_name = 'name'
        our_attr = getattr(our_var, attr_name, "")
        self.compare_attr(
            (our_var, attr_name, our_attr), (None, attr_name, ds_var_name),
            fill_ours=True, fill_ds=False, overwrite_ours=overwrite_ours
        )

    def reconcile_attr(self, our_var, ds_var, our_attr_name, ds_attr_name=None, 
        **kwargs):
        """Compare attribute of a :class:`~src.data_model.DMVariable` (*our_var*) 
        with what's set in the xarray.Dataset (*ds_var*).
        """
        if ds_attr_name is None:
            ds_attr_name = our_attr_name
        our_attr = getattr(our_var, our_attr_name)
        ds_attr = ds_var.attrs.get(ds_attr_name, ATTR_NOT_FOUND)
        self.compare_attr(
            (our_var, our_attr_name, our_attr), (ds_var, ds_attr_name, ds_attr),
            **kwargs
        )

    def reconcile_names(self, our_var, ds, ds_var_name, overwrite_ours=None):
        """Reconcile the name and standard_name attributes between the
        'ground truth' of the dataset we downloaded (*ds_var_name*) and our 
        expectations based on the model's convention (*our_var*).

        Args:
            our_var (:class:`~core.TranslatedVarlistEntry`): Expected attributes
                of the dataset variable, according to the data request.
            ds: xarray DataSet.
            ds_var_name (str): Name of the variable in *ds* we expect to 
                correspond to *our_var*.
            overwrite_ours (bool, default False): If True, always update the name of
                *our_var* to what's found in *ds*.
        """
        # check name
        if ds_var_name not in ds:
            raise util.MetadataError(f"Variable name '{ds_var_name}' not found "
                f"in dataset: ({list(ds.variables)}).")
            # TODO: attempt to match on standard_name?
        self.reconcile_name(our_var, ds_var_name, overwrite_ours=overwrite_ours)
        self.reconcile_attr(our_var, ds[ds_var_name], 'standard_name',
            fill_ours=True, fill_ds=True)

    def reconcile_units(self, our_var, ds_var):
        """Reconcile the units attribute between the 'ground truth' of the 
        dataset we downloaded (*ds_var*) and our expectations based on the 
        model's convention (*our_var*).

        Normal operation is to raise a :class:`~util.UnitsUndefinedError` for missing 
        'units' attributes in the DataSet. This can be reduced to a warning by 
        setting the ``disable_unit_checks`` CLI flag, which sets skip_units=True 
        here.

        Args:
            our_var (:class:`~core.TranslatedVarlistEntry`): Expected attributes
                of the dataset variable, according to the data request.
            ds_var: xarray DataArray.
        """
        # will raise UnitsUndefinedError or log warning if unit attribute missing
        self.check_unit(ds_var)
        # Check equivalence of units: if units inequivalent, raise MetadataEvent
        self.reconcile_attr(our_var, ds_var, 'units', 
            comparison_func=units.units_equivalent,
            fill_ours=True, fill_ds=(self.skip_units)
        )
        # If that passed, check equality of units. Log unequal units as a warning.
        # not an exception, since preprocessor can/will convert them.
        try:
            # test units only, not quantities+units
            self.reconcile_attr(our_var, ds_var, 'units', 
                comparison_func=units.units_equal, 
                fill_ours=True, fill_ds=(self.skip_units)
            )
        except util.MetadataEvent as exc:
            self.log.warning("%s %r.", util.exc_descriptor(exc), exc)
            our_var.units = units.to_cfunits(our_var.units)

    def reconcile_time_units(self, our_var, ds_var):
        """Special case of :meth:`reconcile_units` for the time variable. In
        normal operation we don't know (or need to know) the calendar or 
        reference date (for time units of the form 'days since 1970-01-01'), so
        it's OK to set these from the dataset.

        Args:
            our_var (:class:`~core.TranslatedVarlistEntry`): Expected attributes
                of the dataset variable, according to the data request.
            ds_var: xarray DataArray.
        """
        # will raise UnitsUndefinedError or log warning if unit attribute missing
        self.check_unit(ds_var)
        # Check equivalence of units: if units inequivalent, raise MetadataEvent
        self.reconcile_attr(our_var, ds_var, 'units', 
            comparison_func=units.units_reftime_equivalent,
            fill_ours=True, fill_ds=False, overwrite_ours=None
        )
        self.reconcile_attr(our_var, ds_var, 'units', 
            comparison_func=units.units_equal, 
            fill_ours=True, fill_ds=False, overwrite_ours=True
        )
        self.reconcile_attr(our_var, ds_var, 'calendar',
            fill_ours=True, fill_ds=False, overwrite_ours=True
        )

    def reconcile_scalar_value_and_units(self, our_var, ds_var):
        """Compare scalar coordinate value of a :class:`~src.data_model.DMVariable` 
        (*our_var*) with what's set in the xarray.Dataset (*ds_var*). If there's a 
        discrepancy, log an error but change the entry in *our_var*.
        """
        attr_name = '_scalar_coordinate_value' # placeholder

        def _compare_value_only(our_var, ds_var):
            # may not have units info on ds_var, so only compare the numerical 
            # values and assume the units are the same
            self.compare_attr(
                (our_var, attr_name, our_var.value), 
                (ds_var, attr_name, float(ds_var)),
                fill_ours=True, fill_ds=False
            )
            # update 'units' attribute on ds_var with info from our_var
            self.compare_attr(
                (our_var, attr_name, our_var.units), 
                (ds_var, attr_name, ds_var.attrs.get('units', ATTR_NOT_FOUND)),
                fill_ours=False, fill_ds=True
            )

        def _compare_value_and_units(our_var, ds_var, comparison_func=None):
            # "attribute" to compare is tuple of (numerical value, units string),
            # which is converted to unit-ful object by src.units.to_cfunits() 
            our_attr = (our_var.value, our_var.units)
            ds_attr = (float(ds_var), ds_var.attrs.get('units', ATTR_NOT_FOUND))
            try:
                self.compare_attr(
                    (our_var, attr_name, our_attr), (ds_var, attr_name, ds_attr),
                    comparison_func=comparison_func,
                    fill_ours=True, fill_ds=False
                )
            finally:
                # cleanup placeholder attr if our_var was altered
                if hasattr(our_var, attr_name):
                    our_var.value, new_units = getattr(our_var, attr_name)
                    our_var.units = units.to_cfunits(new_units)
                    self.log.debug("Updated (value, units) of '%s' to (%s, %s).",
                        our_var.name, our_var.value, our_var.units)
                    delattr(our_var, attr_name)

        assert (hasattr(our_var, 'is_scalar') and our_var.is_scalar)
        assert ds_var.size == 1
        # Check equivalence of units: if units inequivalent, raises MetadataEvent
        try:
            _compare_value_and_units(
                our_var, ds_var, 
                comparison_func=units.units_equivalent
            )
        except util.MetadataError:
            # get here if units attr not defined on ds_var
            if self.skip_units:
                _compare_value_only(our_var, ds_var)
                return
            else:
                raise
        # If that passed, check equality of units. Log unequal units as a warning.
        # not an exception, since preprocessor can/will convert them.
        try:
            _compare_value_and_units(
                our_var, ds_var, 
                comparison_func=(lambda x,y: units.units_equal(x,y, rtol=1.0e-5))
            )
        except util.MetadataEvent as exc:
            self.log.warning("%s %r.", util.exc_descriptor(exc), exc)
            our_var.units = units.to_cfunits(our_var.units)

    def reconcile_coord_bounds(self, our_coord, ds, ds_coord_name):
        """Reconcile standard_name and units attributes between the
        'ground truth' of the dataset we downloaded (*ds_var_name*) and our 
        expectations based on the model's convention (*our_var*), for the bounds
        on the dimension coordinate *our_coord*.
        """
        try:
            bounds = ds.cf.get_bounds(ds_coord_name)
        except KeyError:
            # cf accessor could't find associated bounds variable
            our_coord.bounds_var = None
            return

        # Inherit standard_name from our_coord if not present (regardless of 
        # skip_std_name)
        self.reconcile_attr(our_coord, bounds, 'standard_name',
            fill_ours=False, fill_ds=True)
        # Inherit units from our_coord if not present (regardless of skip_units)
        self.reconcile_attr(our_coord, bounds, 'units', 
            comparison_func=units.units_equal,
            fill_ours=False, fill_ds=True
        )
        if our_coord.name != bounds.name:
            self.log.debug("Updating %s for '%s' to value '%s' from dataset.",
                'bounds', our_coord.name, bounds.name)
        our_coord.bounds_var = bounds

    def reconcile_dimension_coords(self, our_var, ds):
        """Reconcile name, standard_name and units attributes between the
        'ground truth' of the dataset we downloaded (*ds_var_name*) and our 
        expectations based on the model's convention (*our_var*), for all 
        dimension coordinates used by *our_var*.

        Args:
            our_var (:class:`~core.TranslatedVarlistEntry`): Expected attributes
                of the dataset variable, according to the data request.
            ds: xarray DataSet.
        """
        for coord in ds.cf.axes(our_var.name).values():
            # .axes() will have thrown TypeError if XYZT axes not all uniquely defined
            assert isinstance(coord, xr.core.dataarray.DataArray)
        # check set of dimension coordinates (array dimensionality) agrees
        our_axes_set = our_var.dim_axes_set
        ds_var = ds[our_var.name]
        ds_axes = ds_var.cf.dim_axes()
        ds_axes_set = ds_var.cf.dim_axes_set
        if our_axes_set != ds_axes_set:
            raise TypeError(f"Variable {our_var.name} has unexpected dimensionality: "
                f" expected axes {list(our_axes_set)}, got {list(ds_axes_set)}.") 
        # check dimension coordinate names, std_names, units, bounds
        for coord in our_var.dim_axes.values():
            ds_coord_name = ds_axes[coord.axis]
            self.reconcile_names(coord, ds, ds_coord_name, overwrite_ours=True)
            if coord.axis == 'T':
                # special case for time coordinate
                self.reconcile_time_units(coord, ds[ds_coord_name])
            else:
                self.reconcile_units(coord, ds[ds_coord_name])
            self.reconcile_coord_bounds(coord, ds, ds_coord_name)
        for c_name in ds_var.dims:
            if ds[c_name].size == 1:
                if c_name == ds_axes['Z']:
                    # mis-identified scalar coordinate
                    self.log.warning(("Dataset has dimension coordinate '%s' of size "
                        "1 not identified as scalar coord."), c_name)
                else:
                    # encounter |X|,|Y| = 1 for single-column models; regardless,
                    # assume user knows what they're doing
                    self.log.debug("Dataset has dimension coordinate '%s' of size 1.")

    def reconcile_scalar_coords(self, our_var, ds):
        """Reconcile name, standard_name and units attributes between the
        'ground truth' of the dataset we downloaded (*ds_var_name*) and our 
        expectations based on the model's convention (*our_var*), for all 
        scalar coordinates used by *our_var*.

        Args:
            our_var (:class:`~core.TranslatedVarlistEntry`): Expected attributes
                of the dataset variable, according to the data request.
            ds: xarray DataSet.
        """
        our_scalars = our_var.scalar_coords
        our_names = [c.name for c in our_scalars]
        our_axes = [c.axis for c in our_scalars]
        ds_var = ds[our_var.name]
        ds_scalars = ds.cf.scalar_coords(our_var.name)
        ds_names = [c.name for c in ds_scalars]
        ds_axes = [c.axis for c in ds_scalars]
        if our_axes and (set(our_axes) != set(['Z'])):
            # should never encounter this
            self.log.error('Scalar coordinates on non-vertical axes not supported.')
        if len(our_axes) != 0 and len(ds_axes) == 0:
            # warning but not necessarily an error if coordinate dims agree
            self.log.debug(("Dataset did not provide any scalar coordinate "
                "information, expected %s."), list(zip(our_names, our_axes)))
        elif our_axes != ds_axes:
            self.log.warning(("Conflict in scalar coordinates for %s: expected ",
                "%s; dataset has %s."), 
                our_var.name, 
                list(zip(our_names, our_axes)), list(zip(ds_names, ds_axes))
            )
        for coord in our_scalars:
            if coord.axis not in ds_var.cf.axes():
                continue # already logged
            ds_coord_name = ds_var.cf.axes().get(coord.axis)
            if ds_coord_name in ds:
                # scalar coord is present in DataSet as a dimension coordinate of
                # size 1.
                if ds[ds_coord_name].size != 1:
                    self.log.error("Dataset has scalar coordinate '%s' of size %d != 1.",
                        ds_coord_name, ds[ds_coord_name].size)
                self.reconcile_names(coord, ds, ds_coord_name, overwrite_ours=True)
                self.reconcile_scalar_value_and_units(our_var, ds[ds_coord_name])
            else:
                # scalar coord has presumably been read from DataSet attribute.
                # At any rate, we only have a PlaceholderScalarCoordinate object, 
                # which only gives us the name. Assume everything else OK.
                self.log.warning(("Dataset only records scalar coordinate '%s' as "
                    "a name attribute; assuming value and units are correct."),
                    ds_coord_name)
                self.reconcile_name(coord, ds_coord_name, overwrite_ours=True)

    def reconcile_variable(self, translated_var, ds):
        """Top-level method for the MDTF-specific dataset validation: attempts to
        reconcile name, standard_name and units attributes for the variable and
        coordinates in *translated_var* (our expectation, based on the DataSource's
        naming convention) with attributes actually present in the Dataset *ds*.
        """
        # check name, std_name, units on variable itself
        self.reconcile_names(translated_var, ds, translated_var.name, 
            overwrite_ours=None)
        self.reconcile_units(translated_var, ds[translated_var.name])
        # check variable's dimension coordinates: names, std_names, units, bounds
        self.reconcile_dimension_coords(translated_var, ds)
        # check variable's scalar coords: names, std_names, units
        self.reconcile_scalar_coords(translated_var, ds)

    # --- Methods for final checks of attrs before preprocessing ---------------

    def check_calendar(self, ds):
        """Checks the 'calendar' attribute has been set correctly for 
        time-dependent data (assumes CF conventions).

        Sets the "calendar" attr on the time coordinate, if it exists, in order
        to be read by the calendar property defined in the cf_xarray accessor.
        """
        def _get_calendar(d):
            self.normalize_calendar(d)
            return d.get('calendar', None)

        t_coords = ds.cf.axes().get('T', [])
        if not t_coords:
            return # assume static data
        elif len(t_coords) > 1:
            self.log.error("Found multiple time axes. Ignoring all but '%s'.", 
                t_coords[0].name)
        t_coord = t_coords[0]

        # normal case: T axis has been parsed into cftime Datetime objects, and
        # the following works successfully.
        cftime_cal = getattr(t_coord.values[0], 'calendar', None)
        # look in other places if that failed:
        if not cftime_cal:
            self.log.warning("cftime calendar info parse failed on '%s'.", 
                t_coord.name)
            cftime_cal = _get_calendar(t_coord.encoding)
        if not cftime_cal:
            cftime_cal = _get_calendar(t_coord.attrs)
        if not cftime_cal:
            cftime_cal = _get_calendar(ds.attrs)
        if not cftime_cal:
            self.log.error("No calendar associated with '%s' found; using '%s'.", 
                t_coord.name, self.fallback_cal)
            cftime_cal = self.fallback_cal
        t_coord.attrs['calendar'] = self.guess_attr(
            'calendar', cftime_cal, _cf_calendars, default=self.fallback_cal)

    def check_standard_name(self, ds_var):
        """Wrapper for :meth:`~DatasetParser.normalize_attr`, specialized to the
        case of getting a variable's standard_name.
        """
        if ds_var.attrs.get('standard_name', ATTR_NOT_FOUND) == ATTR_NOT_FOUND:
            if self.skip_std_name:
                self.log.warning((f"'standard_name' attribute not found on "
                    f"{ds_var.name}."))
            else:
                # normal operation
                raise util.MetadataError(("NetCDF metadata attribute 'standard_name' not "
                    f"found on variable {ds_var.name}. Please provide this attribute "
                    "in input model data or run with --disable_CF_name_checks."))

    def check_unit(self, ds_var):
        """Wrapper for :meth:`~DatasetParser.normalize_attr`, specialized to the 
        case of getting a variable's units.
        """
        if ds_var.attrs.get('units', ATTR_NOT_FOUND) == ATTR_NOT_FOUND:
            if self.skip_units:
                self.log.warning(f"'units' attribute not found on {ds_var.name}.")
            else:
                # normal operation
                raise util.UnitsUndefinedError(("NetCDF metadata attribute 'units' "
                    f"not found on variable {ds_var.name}. Please provide this "
                    "attribute in input model data or run with "
                    "--disable_unit_checks."))

    def check_ds_attrs(self, ds, var=None):
        """Final checking of xarray Dataset attribute dicts before starting
        functions in :mod:`src.preprocessor`.

        Only check attributes on the dependent variable *var_name* and its 
        coordinates: any other netCDF variables in the file are ignored.
        """
        self.check_calendar(ds)
        if var is None:
            # check everything in DataSet
            names_to_check = ds.variables
        else:
            # Only check attributes on the dependent variable var_name and its 
            # coordinates.
            names_to_check = [var.name] + list(ds[var.name].dims)
        for v_name in names_to_check:
            self.check_standard_name(ds[v_name])
            self.check_unit(ds[v_name])

    # --- Top-level methods -----------------------------------------------

    def parse(self, ds, var=None):
        """Calls the above metadata parsing functions in the intended order; 
        intended to be called immediately after the Dataset is opened.

        .. note::
           ``decode_cf=False`` should be passed to the xarray open_dataset 
           method, since that parsing is done here instead.

        - Strip whitespace from attributes as a precaution to avoid malformed 
          metadata.
        - Call xarray's `decode_cf 
          <http://xarray.pydata.org/en/stable/generated/xarray.decode_cf.html>`__,
          using `cftime <https://unidata.github.io/cftime/>`__ to decode 
          CF-compliant date/time axes. 
        - Assign axis labels to dimension coordinates using cf_xarray.
        - Verify that calendar is set correctly.
        - Verify that the name, standard_name and units for the variable and its
            coordinates are set correctly.
        """
        if var is not None:
            self.log = var.log
        self.normalize_ds_attrs(ds)
        ds = xr.decode_cf(ds,         
            decode_coords=True, # parse coords attr
            decode_times=True,
            use_cftime=True     # use cftime instead of np.datetime64
        )
        ds = ds.cf.guess_coord_axis()
        self.restore_attrs_backup(ds)
        self.check_calendar(ds)
        if var is not None:
            self.reconcile_variable(var.translation, ds)
            self.check_ds_attrs(ds, var.translation)
        else:
            self.check_ds_attrs(ds, None)
        return ds
    
    @staticmethod
    def get_unmapped_names(ds):
        """Get a dict whose keys are variable or attribute names referred to by 
        variables in the dataset, but not present in the dataset itself. Values 
        of the dict are sets of names of variables in the dataset that referred 
        to the missing name.
        """
        all_arr_names = set(ds.dims).union(ds.variables)
        all_attr_names = set(getattr(ds, 'attrs', []))
        for name in all_arr_names:
            all_attr_names.update(getattr(ds[name], 'attrs', []))

        # NOTE: will currently fail on CAM/CESM P0. Where do they store it?
        missing_refs = dict()
        lookup = collections.defaultdict(set)
        for name in all_arr_names:
            refs = set(getattr(ds[name], 'dims', []))
            refs.update(itertools.chain.from_iterable(
                ds.cf.get_associated_variable_names(name).values()
            ))
            refs.update(ds[name].cf.formula_terms.values())
            for ref in refs:
                lookup[ref].add(name)
        for ref in lookup:
            if (ref not in all_arr_names) and (ref not in all_attr_names):
                missing_refs[ref] = lookup[ref]
        return missing_refs
