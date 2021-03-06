import os

import click
import dask
import numpy as np
import requests
from core import Builder, extract_attr_with_regex, get_asset_list, reverse_filename_format
from dask.diagnostics import ProgressBar


def cmip6_parser(filepath):
    """
    Extract attributes of a file using information from CMI6 DRS.
    References

    CMIP6 DRS: http://goo.gl/v1drZl
    Controlled Vocabularies (CVs) for use in CMIP6: https://github.com/WCRP-CMIP/CMIP6_CVs
    Directory structure =

    <mip_era>/
        <activity_id>/
            <institution_id>/
                <source_id>/
                    <experiment_id>/
                        <member_id>/
                            <table_id>/
                                <variable_id>/
                                    <grid_label>/
                                        <version>
    file name = <variable_id>_<table_id>_<source_id>_<experiment_id >_<member_id>_<grid_label>[_<time_range>].nc For time-invariant fields, the last segment (time_range) above is omitted. Example when there is no sub-experiment: tas_Amon_GFDL-CM4_historical_r1i1p1f1_gn_196001-199912.nc Example with a sub-experiment: pr_day_CNRM-CM6-1_dcppA-hindcast_s1960-r2i1p1f1_gn_198001-198412.nc
    """
    basename = os.path.basename(filepath)
    filename_template = '{variable_id}_{table_id}_{source_id}_{experiment_id}_{member_id}_{grid_label}_{time_range}.nc'

    gridspec_template = (
        '{variable_id}_{table_id}_{source_id}_{experiment_id}_{member_id}_{grid_label}.nc'
    )
    templates = [filename_template, gridspec_template]
    fileparts = reverse_filename_format(basename, templates=templates)
    try:
        parent = os.path.dirname(filepath).strip('/')
        parent_split = parent.split(f"/{fileparts['source_id']}/")
        part_1 = parent_split[0].strip('/').split('/')
        grid_label = parent.split(f"/{fileparts['variable_id']}/")[1].strip('/').split('/')[0]
        fileparts['grid_label'] = grid_label
        fileparts['activity_id'] = part_1[-2]
        fileparts['institution_id'] = part_1[-1]
        version_regex = r'v\d{4}\d{2}\d{2}|v\d{1}'
        version = extract_attr_with_regex(parent, regex=version_regex) or 'v0'
        fileparts['version'] = version
        fileparts['path'] = filepath
        if fileparts['member_id'].startswith('s'):
            fileparts['dcpp_init_year'] = float(fileparts['member_id'].split('-')[0][1:])
            fileparts['member_id'] = fileparts['member_id'].split('-')[-1]
        else:
            fileparts['dcpp_init_year'] = np.nan

    except Exception:
        pass

    return fileparts


def cmip5_parser(filepath):
    """ Extract attributes of a file using information from CMIP5 DRS.
    Notes
    -----
    Reference:
    - CMIP5 DRS: https://pcmdi.llnl.gov/mips/cmip5/docs/cmip5_data_reference_syntax.pdf?id=27
    """

    freq_regex = r'/3hr/|/6hr/|/day/|/fx/|/mon/|/monClim/|/subhr/|/yr/'
    realm_regex = r'aerosol|atmos|land|landIce|ocean|ocnBgchem|seaIce'
    version_regex = r'v\d{4}\d{2}\d{2}|v\d{1}'

    file_basename = os.path.basename(filepath)

    filename_template = (
        '{variable}_{mip_table}_{model}_{experiment}_{ensemble_member}_{temporal_subset}.nc'
    )
    gridspec_template = '{variable}_{mip_table}_{model}_{experiment}_{ensemble_member}.nc'

    templates = [filename_template, gridspec_template]
    fileparts = reverse_filename_format(file_basename, templates)
    frequency = extract_attr_with_regex(filepath, regex=freq_regex, strip_chars='/')
    realm = extract_attr_with_regex(filepath, regex=realm_regex)
    version = extract_attr_with_regex(filepath, regex=version_regex) or 'v0'
    fileparts['frequency'] = frequency
    fileparts['modeling_realm'] = realm
    fileparts['version'] = version
    fileparts['path'] = filepath
    try:
        part1, part2 = os.path.dirname(filepath).split(fileparts['experiment'])
        part1 = part1.strip('/').split('/')
        fileparts['institute'] = part1[-2]
        fileparts['product_id'] = part1[-3]
    except Exception:
        pass

    return fileparts


def _pick_latest_version(df):
    import itertools

    grpby = list(set(df.columns.tolist()) - {'path', 'version'})
    groups = df.groupby(grpby)

    @dask.delayed
    def _pick_latest_v(group):
        idx = []
        if group.version.nunique() > 1:
            idx = group.sort_values(by=['version'], ascending=False).index[1:].values.tolist()
        return idx

    idx_to_remove = [_pick_latest_v(group) for _, group in groups]
    print('Getting latest version...\n')
    with ProgressBar():
        idx_to_remove = dask.compute(*idx_to_remove)

    idx_to_remove = list(itertools.chain(*idx_to_remove))
    df = df.drop(index=idx_to_remove)
    print('\nDone....\n')
    return df


def build_cmip(
    root_path,
    cmip_version,
    depth=4,
    columns=None,
    exclude_patterns=['*/files/*', '*/latest/*'],
    pick_latest_version=False,
):
    parsers = {'6': cmip6_parser, '5': cmip5_parser}
    cmip_columns = {
        '6': [
            'activity_id',
            'institution_id',
            'source_id',
            'experiment_id',
            'member_id',
            'table_id',
            'variable_id',
            'grid_label',
            'dcpp_init_year',
            'version',
            'time_range',
            'path',
        ],
        '5': [
            'product_id',
            'institute',
            'model',
            'experiment',
            'frequency',
            'modeling_realm',
            'mip_table',
            'ensemble_member',
            'variable',
            'temporal_subset',
            'version',
            'path',
        ],
    }

    filelist = get_asset_list(root_path, depth=depth)
    cmip_version = str(cmip_version)
    if columns is None:
        columns = cmip_columns[cmip_version]
    b = Builder(columns, exclude_patterns)
    df = b(filelist, parsers[cmip_version])

    if cmip_version == '6':
        # Some entries are invalid: Don't conform to the CMIP6 Data Reference Syntax
        cmip6_activity_id_url = (
            'https://raw.githubusercontent.com/WCRP-CMIP/CMIP6_CVs/master/CMIP6_activity_id.json'
        )
        resp = requests.get(cmip6_activity_id_url)
        activity_ids = list(resp.json()['activity_id'].keys())
        # invalids = df[~df.activity_id.isin(activity_ids)]
        df = df[df.activity_id.isin(activity_ids)]
    if pick_latest_version:
        df = _pick_latest_version(df)
    return df.sort_values(by=['path'])


@click.command()
@click.option(
    '--root-path', type=click.Path(exists=True), help='Root path of the CMIP project output.'
)
@click.option(
    '-d',
    '--depth',
    default=4,
    type=int,
    show_default=True,
    help='Recursion depth. Recursively walk root_path to a specified depth',
)
@click.option(
    '--pick-latest-version',
    default=False,
    is_flag=True,
    show_default=True,
    help='Whether to only catalog lastest version of data assets or keep all versions',
)
@click.option('-v', '--cmip-version', type=int, help='CMIP phase (e.g. 5 for CMIP5 or 6 for CMIP6)')
@click.option('--csv-filepath', type=str, help='File path to use when saving the built catalog')
def cli(root_path, depth, pick_latest_version, cmip_version, csv_filepath):

    if cmip_version not in set([5, 6]):
        raise ValueError(
            f'cmip_version = {cmip_version} is not valid. Valid options include: 5 and 6.'
        )

    if csv_filepath is None:
        raise ValueError("Please provide csv-filepath. e.g.: './cmip5.csv.gz'")

    df = build_cmip(root_path, cmip_version, depth=depth, pick_latest_version=pick_latest_version)

    df.to_csv(csv_filepath, compression='gzip', index=False)


if __name__ == '__main__':
    cli()
