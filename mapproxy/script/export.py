# This file is part of the MapProxy project.
# Copyright (C) 2012 Omniscale <http://omniscale.de>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import with_statement, division

import os
import re
import shlex
import sys
import optparse

import yaml

from mapproxy.srs import SRS
from mapproxy.config.coverage import load_coverage
from mapproxy.config.loader import (
    load_configuration, ConfigurationError,
    CacheConfiguration, GridConfiguration,
)
from mapproxy.util.coverage import  BBOXCoverage
from mapproxy.seed.util import ProgressLog, format_bbox
from mapproxy.seed.seeder import SeedTask, seed_task
from mapproxy.config import spec as conf_spec
from mapproxy.util.ext.dictspec.validator import validate, ValidationError


def parse_levels(level_str):
    """
    >>> parse_levels('1,2,3,6')
    [1, 2, 3, 6]
    >>> parse_levels('1..6')
    [1, 2, 3, 4, 5, 6]
    >>> parse_levels('1..6, 8, 9, 13..14')
    [1, 2, 3, 4, 5, 6, 8, 9, 13, 14]
    """
    levels = set()
    for part in level_str.split(','):
        part = part.strip()
        if re.match('\d+..\d+', part):
            from_level, to_level = part.split('..')
            levels.update(range(int(from_level), int(to_level) + 1))
        else:
            levels.add(int(part))

    return sorted(levels)

def parse_grid_definition(definition):
    """
    >>> parse_grid_definition("res=[10000,1000,100,10] srs=EPSG:4326 bbox=5,50,10,60")
    {'res': [10000, 1000, 100, 10], 'bbox': '5,50,10,60', 'srs': 'EPSG:4326'}
    """
    args = shlex.split(definition)
    grid_conf = {}
    for arg in args:
        key, value = arg.split('=')
        value = yaml.load(value)
        grid_conf[key] = value

    validate(conf_spec.grid_opts, grid_conf)
    return grid_conf

def supports_tiled_access(mgr):
    if len(mgr.sources) == 1 and getattr(mgr.sources[0], 'supports_meta_tiles') == False:
        return True
    return False


def format_export_task(task, custom_grid):
    info = []
    if custom_grid:
        grid = "custom grid"
    else:
        grid = "grid '%s'" % task.md['grid_name']

    info.append("Exporting cache '%s' to '%s' with %s in %s" % (
                 task.md['cache_name'], task.md['dest'], grid, task.grid.srs.srs_code))
    if task.coverage:
        info.append('  Limited to: %s (EPSG:4326)' % (format_bbox(task.coverage.extent.llbbox), ))
    info.append('  Levels: %s' % (task.levels, ))

    return '\n'.join(info)

def export_command(args=None):
    parser = optparse.OptionParser("%prog grids [options] mapproxy_conf")
    parser.add_option("-f", "--mapproxy-conf", dest="mapproxy_conf",
        help="MapProxy configuration")

    parser.add_option("--source", dest="source",
        help="source to export (source or cache)")

    parser.add_option("--grid",
        help="grid for export. either the name of an existing grid or "
        "the grid definition as a string")

    parser.add_option("--dest",
        help="destination of the export (directory or filename)")

    parser.add_option("--type",
        help="type of the export format")

    parser.add_option("--levels",
        help="levels to export: e.g 1,2,3 or 1..10")

    parser.add_option("--fetch-missing-tiles", dest="fetch_missing_tiles",
        action='store_true', default=False,
        help="if missing tiles should be fetched from the sources")

    parser.add_option("--force",
        action='store_true', default=False,
        help="overwrite/append to existing --dest files/directories")

    parser.add_option("-n", "--dry-run",
        action="store_true", default=False,
        help="do not export, just print output")

    parser.add_option("-c", "--concurrency", type="int",
        dest="concurrency", default=1,
        help="number of parallel export processes")

    parser.add_option("--coverage",
        help="the coverage for the export as a BBOX string, WKT file "
        "or OGR datasource")
    parser.add_option("--srs",
        help="the SRS of the coverage")
    parser.add_option("--where",
        help="filter for OGR coverages")

    from mapproxy.script.util import setup_logging
    import logging
    setup_logging(logging.WARN)

    if args:
        args = args[1:] # remove script name

    (options, args) = parser.parse_args(args)

    if not options.mapproxy_conf:
        if len(args) != 1:
            parser.print_help()
            sys.exit(1)
        else:
            options.mapproxy_conf = args[0]

    required_options = ['mapproxy_conf', 'grid', 'source', 'dest', 'levels']
    for required in required_options:
        if not getattr(options, required):
            print >>sys.stderr, 'ERROR: missing required option --%s' % required.replace('_', '-')
            parser.print_help()
            sys.exit(1)

    try:
        conf = load_configuration(options.mapproxy_conf)
    except IOError, e:
        print >>sys.stderr, 'ERROR: ', "%s: '%s'" % (e.strerror, e.filename)
        sys.exit(2)
    except ConfigurationError, e:
        print >>sys.stderr, 'ERROR: invalid configuration (see above)'
        sys.exit(2)


    if '=' in options.grid:
        try:
            grid_conf = parse_grid_definition(options.grid)
        except ValidationError, ex:
            print >>sys.stderr, 'ERROR: invalid grid configuration'
            for error in ex.errors:
                print >>sys.stderr, ' ', error
            sys.exit(2)
        except ValueError:
            print >>sys.stderr, 'ERROR: invalid grid configuration'
            sys.exit(2)
        options.grid = 'tmp_mapproxy_export_grid'
        grid_conf['name'] = options.grid
        custom_grid = True
        conf.grids[options.grid] = GridConfiguration(grid_conf, conf)
    else:
        custom_grid = False

    if os.path.exists(options.dest) and not options.force:
        print >>sys.stderr, 'ERROR: destination exists, remove first or use --force'
        sys.exit(2)


    cache_conf = {
        'name': 'export',
        'grids': [options.grid],
        'sources': [options.source],
    }
    if options.type == 'mbtile':
        cache_conf['cache'] = {
            'type': 'mbtiles',
            'filename': options.dest,
        }
    elif options.type in ('tc', 'mapproxy'):
        cache_conf['cache'] = {
            'type': 'file',
            'directory': options.dest,
        }
    elif options.type in ('tms', None): # default
        cache_conf['cache'] = {
            'type': 'file',
            'directory_layout': 'tms',
            'directory': options.dest,
        }
    else:
        print >>sys.stderr, 'ERROR: unsupported --type %s' % (options.type, )
        sys.exit(2)

    if not options.fetch_missing_tiles:
        for source in conf.sources.values():
            source.conf['seed_only'] = True

    tile_grid, extent, mgr = CacheConfiguration(cache_conf, conf).caches()[0]


    levels = parse_levels(options.levels)
    if levels[-1] >= tile_grid.levels:
        print >>sys.stderr, 'ERROR: destination grid only has %d levels' % tile_grid.levels
        sys.exit(2)

    if options.srs:
        srs = SRS(options.srs)
    else:
        srs = tile_grid.srs

    if options.coverage:
        seed_coverage = load_coverage(
            {'datasource': options.coverage, 'srs': srs, 'where': options.where},
            base_path=os.getcwd())
    else:
        seed_coverage = BBOXCoverage(tile_grid.bbox, tile_grid.srs)

    if not supports_tiled_access(mgr):
        print >>sys.stderr, 'WARN: grids are incompatible. needs to scale/reproject tiles for export.'

    md = dict(name='export', cache_name='cache', grid_name=options.grid, dest=options.dest)
    task = SeedTask(md, mgr, levels, None, seed_coverage)

    print format_export_task(task, custom_grid=custom_grid)

    logger = ProgressLog(verbose=True, silent=False)
    try:
        seed_task(task, progress_logger=logger, dry_run=options.dry_run,
             concurrency=options.concurrency)
    except KeyboardInterrupt:
        print >>sys.stderr, 'stopping...'
        sys.exit(2)
