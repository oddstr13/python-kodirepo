#!/usr/bin/env python
r"""
Create a Kodi add-on repository from add-on sources

This tool extracts Kodi add-ons from their respective locations and
copies the appropriate files into a Kodi add-on repository. Each add-on
is placed in its own directory. Each contains the add-on metadata files
and a zip archive. In addition, the repository catalog "addons.xml" is
placed in the repository folder.

Each add-on location is either a local path or a URL. If it is a local
path, it can be to either an add-on folder or an add-on ZIP archive. If
it is a URL, it should be to a Git repository and it should use the
format:
    REPOSITORY_URL#BRANCH:PATH
The first segment is the Git URL that would be used to clone the
repository, (e.g.,
"https://github.com/chadparry/kodi-repository.chad.parry.org.git").
That is followed by an optional "#" sign and a branch or tag name,
(e.g. "release-1.0"). If no branch name is specified, then the default
is the repository's currently active branch, which is the same default
as git-clone. Next comes an optional ":" sign and path. The path
denotes the location of the add-on within the repository. If no path is
specified, then the default is ".".

For example, if you are in the directory that should contain addons.xml
and you just copied a new version of the only add-on
"repository.chad.parry.org" to a subdirectory, then you can create or
update the addons.xml file with this command:

    ./create_repository.py repository.chad.parry.org

As another example, here is the command that generates Chad Parry's
Repository:

    create_repository.py \
        --datadir=~/html/software/kodi \
        --compressed \
        https://github.com/chadparry\
/kodi-repository.chad.parry.org.git:repository.chad.parry.org \
        https://github.com/chadparry\
/kodi-plugin.program.remote.control.browser.git\
:plugin.program.remote.control.browser

This script has been tested with Python 2.7.6 and Python 3.4.3. It
depends on the GitPython module.
"""

__author__ = "Chad Parry"
__contact__ = "github@chad.parry.org"
__copyright__ = "Copyright 2016-2018 Chad Parry"
__license__ = "GNU GENERAL PUBLIC LICENSE. Version 2, June 1991"
from .version import version as __version__


import collections
import gzip
import hashlib
import io
import os
import re
import shutil
import sys
import tempfile
import threading
import xml.etree.ElementTree
import zipfile
import logging

import git
import click
import click_log
import semantic_version


logger = logging.getLogger("kodirepo")
click_log.basic_config(logger)


AddonMetadata = collections.namedtuple(
    'AddonMetadata', ('id', 'version', 'root'))
WorkerResult = collections.namedtuple(
    'WorkerResult', ('addon_metadata', 'exc_info'))
AddonWorker = collections.namedtuple('AddonWorker', ('thread', 'result_slot'))


INFO_BASENAME = 'addon.xml'
METADATA_BASENAMES = (
    INFO_BASENAME,
    'icon.png',
    'fanart.jpg',
    'LICENSE.txt')


def get_archive_basename(addon_metadata):
    return '{}-{}.zip'.format(addon_metadata.id, addon_metadata.version)


def get_metadata_basenames(addon_metadata):
    return ([(basename, basename) for basename in METADATA_BASENAMES] +
            [(
                'changelog.txt',
                'changelog-{}.txt'.format(addon_metadata.version))])


def is_url(addon_location):
    return bool(re.match('[A-Za-z0-9+.-]+://.', addon_location))


class AddonVersion(semantic_version.Version):
    # The specification for version numbers is at http://semver.org/.
    # The Kodi documentation at
    # http://kodi.wiki/index.php?title=Addon.xml#How_versioning_works
    # adds a twist by recommending a tilde instead of a hyphen.
    # https://github.com/xbmc/xbmc/blob/master/xbmc/addons/AddonVersion.cpp#L20L24

    @classmethod
    def parse(cls, version_string, partial=False, coerce=False):
        return super().parse(version_string.replace('~', '-'), partial=partial, coerce=coerce)

    def __str__(self):
        return super().__str__().replace('-', '~')


def parse_metadata(metadata_file):
    # Parse the addon.xml metadata.
    try:
        tree = xml.etree.ElementTree.parse(metadata_file)
    except IOError:
        raise RuntimeError('Cannot open addon metadata: {}'.format(metadata_file))

    root = tree.getroot()

    addon_metadata = AddonMetadata(
        root.get('id'),
        AddonVersion(root.get('version')),
        root
    )
    root.set('version', addon_metadata.version)

    logger.debug("Parsed %s v%s from %s", addon_metadata.id, addon_metadata.version, metadata_file)

    # Validate the add-on ID.
    # https://kodi.wiki/index.php?title=Addon.xml#id_attribute
    if (addon_metadata.id is None or
            re.search('[^a-z0-9._-]', addon_metadata.id)):
        raise RuntimeError('Invalid addon ID: {}'.format(addon_metadata.id))

    return addon_metadata


def generate_checksum(archive_path, is_binary=True, checksum_path_opt=None):
    checksum_path = ('{}.md5'.format(archive_path)
        if checksum_path_opt is None else checksum_path_opt)
    checksum_dirname = os.path.dirname(checksum_path)
    archive_relpath = os.path.relpath(archive_path, checksum_dirname)

    checksum = hashlib.md5()
    with open(archive_path, 'rb') as archive_contents:
        for chunk in iter(lambda: archive_contents.read(2**12), b''):
            checksum.update(chunk)
    digest = checksum.hexdigest()

    binary_marker = '*' if is_binary else ' '
    # Force a UNIX line ending, like the md5sum utility.
    with io.open(checksum_path, 'w', newline='\n') as sig:
        sig.write(u'{} {}{}\n'.format(digest, binary_marker, archive_relpath))


def copy_metadata_files(source_folder, addon_target_folder, addon_metadata):
    for (source_basename, target_basename) in get_metadata_basenames(
            addon_metadata):
        source_path = os.path.join(source_folder, source_basename)
        if os.path.isfile(source_path):
            shutil.copyfile(
                source_path,
                os.path.join(addon_target_folder, target_basename))


def fetch_addon_from_git(addon_location, target_folder):
    # Parse the format "REPOSITORY_URL#BRANCH:PATH". The colon is a delimiter
    # unless it looks more like a scheme, (e.g., "http://").
    match = re.match(
        '((?:[A-Za-z0-9+.-]+://)?.*?)(?:#([^#]*?))?(?::([^:]*))?$',
        addon_location)
    (clone_repo, clone_branch, clone_path_option) = match.group(1, 2, 3)
    clone_path = './' if clone_path_option is None else clone_path_option

    # Create a temporary folder for the git clone.
    clone_folder = tempfile.mkdtemp('-repo')
    try:
        # Check out the sources.
        cloned = git.Repo.clone_from(clone_repo, clone_folder)
        if clone_branch is not None:
            cloned.git.checkout(clone_branch)
        clone_source_folder = os.path.join(clone_folder, clone_path)

        metadata_path = os.path.join(clone_source_folder, INFO_BASENAME)
        addon_metadata = parse_metadata(metadata_path)
        addon_target_folder = os.path.join(target_folder, addon_metadata.id)

        # Create the compressed add-on archive.
        if not os.path.isdir(addon_target_folder):
            os.mkdir(addon_target_folder)
        archive_path = os.path.join(
            addon_target_folder, get_archive_basename(addon_metadata))
        with open(archive_path, 'wb') as archive:
            cloned.archive(
                archive,
                treeish='HEAD:{}'.format(clone_path),
                prefix='{}/'.format(addon_metadata.id),
                format='zip')
        generate_checksum(archive_path)

        copy_metadata_files(
            clone_source_folder, addon_target_folder, addon_metadata)

        return addon_metadata
    finally:
        shutil.rmtree(clone_folder, ignore_errors=False)


def fetch_addon_from_folder(raw_addon_location, target_folder):
    addon_location = os.path.expanduser(raw_addon_location)
    metadata_path = os.path.join(addon_location, INFO_BASENAME)
    addon_metadata = parse_metadata(metadata_path)
    addon_target_folder = os.path.join(target_folder, addon_metadata.id)

    # Create the compressed add-on archive.
    if not os.path.isdir(addon_target_folder):
        os.mkdir(addon_target_folder)
    archive_path = os.path.join(
        addon_target_folder, get_archive_basename(addon_metadata))
    with zipfile.ZipFile(
            archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for (root, dirs, files) in os.walk(addon_location):
            relative_root = os.path.join(
                addon_metadata.id,
                os.path.relpath(root, addon_location))
            for relative_path in files:
                archive.write(
                    os.path.join(root, relative_path),
                    os.path.join(relative_root, relative_path))
    generate_checksum(archive_path)

    if not os.path.samefile(addon_location, addon_target_folder):
        copy_metadata_files(
            addon_location, addon_target_folder, addon_metadata)

    return addon_metadata


def fetch_addon_from_zip(raw_addon_location, target_folder):
    addon_location = os.path.expanduser(raw_addon_location)
    with zipfile.ZipFile(
            addon_location, compression=zipfile.ZIP_DEFLATED) as archive:
        # Find out the name of the archive's root folder.
        roots = frozenset(
            next(iter(path.split(os.path.sep)), '')
            for path in archive.namelist())
        if len(roots) != 1:
            raise RuntimeError('Archive should contain one directory')
        root = next(iter(roots))

        metadata_file = archive.open(os.path.join(root, INFO_BASENAME))
        addon_metadata = parse_metadata(metadata_file)
        addon_target_folder = os.path.join(target_folder, addon_metadata.id)

        # Copy the metadata files.
        if not os.path.isdir(addon_target_folder):
            os.mkdir(addon_target_folder)
        for (source_basename, target_basename) in get_metadata_basenames(
                addon_metadata):
            try:
                source_file = archive.open(os.path.join(root, source_basename))
            except KeyError:
                continue
            with open(
                    os.path.join(addon_target_folder, target_basename),
                    'wb') as target_file:
                shutil.copyfileobj(source_file, target_file)

    # Copy the archive.
    archive_basename = get_archive_basename(addon_metadata)
    archive_path = os.path.join(addon_target_folder, archive_basename)
    if (not os.path.samefile(
            os.path.dirname(addon_location), addon_target_folder) or
            os.path.basename(addon_location) != archive_basename):
        shutil.copyfile(addon_location, archive_path)
    generate_checksum(archive_path)

    return addon_metadata


def fetch_addon(addon_location, target_folder, result_slot):
    logger.debug("Reading add-on from %r", addon_location)
    try:
        if is_url(addon_location):
            addon_metadata = fetch_addon_from_git(
                addon_location, target_folder)
        elif os.path.isdir(addon_location):
            addon_metadata = fetch_addon_from_folder(
                addon_location, target_folder)
        elif os.path.isfile(addon_location):
            addon_metadata = fetch_addon_from_zip(
                addon_location, target_folder)
        else:
            raise RuntimeError('Path not found: {}'.format(addon_location))
        result_slot.append(WorkerResult(addon_metadata, None))
    except RuntimeError:
        result_slot.append(WorkerResult(None, sys.exc_info()))


def get_addon_worker(addon_location, target_folder):
    result_slot = []
    thread = threading.Thread(target=lambda: fetch_addon(
        addon_location, target_folder, result_slot))
    return AddonWorker(thread, result_slot)


def parse_repo(metadata_file):
    # Parse the addons.xml metadata.
    try:
        tree = xml.etree.ElementTree.parse(metadata_file)
    except IOError:
        raise RuntimeError('Cannot open addon metadata: {}'.format(metadata_file))

    root = tree.getroot()

    for addon_el in root:

        addon_metadata = AddonMetadata(
            addon_el.get('id'),
            AddonVersion(addon_el.get('version')),
            addon_el
        )
        addon_el.set('version', addon_metadata.version)

        logger.debug("Parsed %s v%s from addons.xml", addon_metadata.id, addon_metadata.version)

        # Validate the add-on ID.
        # https://kodi.wiki/index.php?title=Addon.xml#id_attribute
        if (addon_metadata.id is None or
                re.search('[^a-z0-9._-]', addon_metadata.id)):
            raise RuntimeError('Invalid addon ID: {}'.format(addon_metadata.id))

        yield addon_metadata


def create_repository(
        addon_locations,
        target_folder,
        info_path,
        checksum_path,
        is_compressed,
        no_parallel,
        clobber=True):

    if os.path.exists(info_path):
        metadata = list(parse_repo(info_path))
    else:
        logger.warning("Could not find existing addons.xml, creating new at %r", info_path)
        metadata = []

    # Create the target folder.
    if not os.path.isdir(target_folder):
        os.mkdir(target_folder)

    # Fetch all the add-on sources in parallel.
    workers = [
        get_addon_worker(addon_location, target_folder)
        for addon_location in addon_locations]
    if no_parallel:
        for worker in workers:
            worker.thread.run()
    else:
        for worker in workers:
            worker.thread.start()
        for worker in workers:
            worker.thread.join()

    # Collect the results from all the threads.
    new_metadata = []
    for worker in workers:
        try:
            result = next(iter(worker.result_slot))
        except StopIteration:
            raise RuntimeError('Addon worker did not report result')
        if result.exc_info is not None:
            raise result.exc_info[1]
        new_metadata.append(result.addon_metadata)

    for addon_metadata in new_metadata:
        version_exists = list(filter(lambda m: addon_metadata.id == m.id and addon_metadata.version == m.version, metadata))
        if version_exists:
            if clobber:
                logger.warning("Clobbering %s v%s", addon_metadata.id, addon_metadata.version)
                for old in version_exists:
                    metadata.remove(old)
            else:
                raise RuntimeError("Refusing to overwrite %s v%s. See --clobber." % (addon_metadata.id, addon_metadata.version))

        logger.info("Adding %s v%s", addon_metadata.id, addon_metadata.version)
        metadata.append(addon_metadata)

    # Sort items by id, then reversed version
    metadata.sort(key=lambda m: m.version, reverse=True)
    metadata.sort(key=lambda m: m.id)

    # Generate the addons.xml file.
    root = xml.etree.ElementTree.Element('addons')
    for addon_metadata in metadata:
        logger.info("Writing %s v%s to addons.xml", addon_metadata.id, addon_metadata.version)
        root.append(addon_metadata.root)
    tree = xml.etree.ElementTree.ElementTree(root)
    if is_compressed:
        info_file = gzip.open(info_path, 'wb')
    else:
        info_file = open(info_path, 'wb')
    with info_file:
        tree.write(info_file, encoding='UTF-8', xml_declaration=True)
    is_binary = is_compressed
    generate_checksum(info_path, is_binary, checksum_path)


class AddonSourceParam(click.ParamType):
    is_path = click.Path(exists=True, file_okay=True, dir_okay=True, readable=True)

    def validate(self, value, param, ctx):
        return is_url(value) or self.is_path(value, param, ctx)


@click.group()
@click.version_option()
@click_log.simple_verbosity_option(logger)
def cli():
    pass


@cli.command('add')
@click.option('--datadir', '-d',
    default='.',
    type=click.Path(exists=False, file_okay=False, dir_okay=True, writable=True),
    help='Path to place the add-ons [current directory]',
)
@click.option('--info', '-i',
    help='''Path for the addons.xml file [DATADIR/addons.xml or
            DATADIR/addons.xml.gz if compressed]''',
)
@click.option('--checksum', '-c',
    help='Path for the addons.xml.md5 file [INFO.md5]'
)
@click.option('--compress/--no-compress', '-z/', '--compressed/',
    default=False,
    help='Compress addons.xml with gzip'
)
@click.option('--parallel/--no-parallel', ' /-n',
    default=True,
    show_default=True,
    help='Build add-on sources in parallel'
)
@click.option('--clobber/--no-clobber',
    default=False,
    show_default=True,
    help='Overwrite existing versions'
)
@click.argument('addons',
    nargs=-1,
    required=True,
    metavar='ADDON...',
    type=AddonSourceParam(),
)
def main(datadir, info, checksum, compress, parallel, clobber, addons):
    '''
    Create a Kodi add-on repository from add-on sources

    ADDON can be one or more;
    Path to local folder,
    Path to add-on zip file, or
    Git repo URL in the format URL#BRANCH:PATH
    '''

    data_path = os.path.expanduser(datadir)

    if not info:
        if compress:
            info_basename = 'addons.xml.gz'
        else:
            info_basename = 'addons.xml'
        info_path = os.path.join(data_path, info_basename)
    else:
        info_path = os.path.expanduser(info)

    if checksum:
        checksum_path = os.path.expanduser(checksum)
    else:
        checksum_path = '{}.md5'.format(info_path)

    try:
        create_repository(addons, data_path, info_path, checksum_path, compress, not parallel, clobber)
    except RuntimeError as e:
        logger.error(e)
        raise click.Abort()


if __name__ == "__main__":
    main()
