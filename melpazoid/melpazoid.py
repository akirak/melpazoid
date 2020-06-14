# -*- coding: utf-8 -*-
"""Entrypoint to melpazoid."""
import configparser
import functools
import glob
import io  # noqa: F401 -- used by doctests
import operator
import os
import re
import requests
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Iterator, List, TextIO, Tuple

DEBUG = False  # eagerly load installed packages, etc.
_RETURN_CODE = 0  # eventual return code when run as script
_PKG_SUBDIR = 'pkg'  # name of directory for package's files

# define the colors of the report (or none), per https://no-color.org
# https://misc.flogisoft.com/bash/tip_colors_and_formatting
NO_COLOR = os.environ.get('NO_COLOR', False)
CLR_OFF = '' if NO_COLOR else '\033[0m'
CLR_ERROR = '' if NO_COLOR else '\033[31m'
CLR_WARN = '' if NO_COLOR else '\033[33m'
CLR_INFO = '' if NO_COLOR else '\033[32m'
CLR_ULINE = '' if NO_COLOR else '\033[4m'

GITHUB_API = 'https://api.github.com/repos'
MELPA_PR = r'https://github.com/melpa/melpa/pull/([0-9]+)'
MELPA_PULL_API = f"{GITHUB_API}/melpa/melpa/pulls"
MELPA_RECIPES = f"{GITHUB_API}/melpa/melpa/contents/recipes"

# Valid licenses and their names according to the GitHub API
# TODO: complete this list!
VALID_LICENSES_GITHUB = {
    'Apache License 2.0',
    'GNU Affero General Public License v3.0',
    'GNU General Public License v2.0',
    'GNU General Public License v3.0',
    'GNU Lesser General Public License v3.0',
    'ISC License',
    'MIT License',
    'The Unlicense',
}


def _run_checks(
    recipe: str,  # e.g. of the form (my-package :repo ...)
    elisp_dir: str,  # where the package is on this machine
    clone_address: str = None,  # optional repo address
    pr_data: dict = None,  # optional data from the PR
):
    """Entrypoint for running all checks."""
    if not validate_recipe(recipe):
        _fail(f"Recipe '{recipe}' appears to be invalid")
        return
    try:
        shutil.rmtree(_PKG_SUBDIR)
    except FileNotFoundError:
        pass
    files = _files_in_recipe(recipe, elisp_dir)
    use_default_recipe = files == _files_in_default_recipe(recipe, elisp_dir)
    for ii, file in enumerate(files):
        target = os.path.basename(file) if file.endswith('.el') else file
        target = os.path.join(_PKG_SUBDIR, target)
        os.makedirs(os.path.join(_PKG_SUBDIR, os.path.dirname(file)), exist_ok=True)
        subprocess.run(['mv', os.path.join(elisp_dir, file), target])
        files[ii] = target
    _write_requirements(files, recipe)
    check_containerized_build(files, recipe)
    if os.environ.get('EXIST_OK', '').lower() != 'true':
        print_similar_packages(package_name(recipe))
    print_packaging(files, recipe, use_default_recipe, elisp_dir, clone_address)
    if clone_address and pr_data:
        _print_pr_footnotes(clone_address, pr_data, recipe)


def _return_code(return_code: int = None) -> int:
    """Return (and optionally set) the current return code.
    If return_code matches env var EXPECT_ERROR, return 0 --
    this is useful for running CI checks on melpazoid itself.
    """
    global _RETURN_CODE
    if return_code is not None:
        _RETURN_CODE = return_code
    expect_error = int(os.environ.get('EXPECT_ERROR', 0))
    return 0 if _RETURN_CODE == expect_error else _RETURN_CODE


def validate_recipe(recipe: str) -> bool:
    """Validate whether the recipe looks correct.
    >>> validate_recipe('(abc :repo "xyz" :fetcher github) ; abc recipe!')
    True
    >>> validate_recipe('??')
    False
    """
    tokenized_recipe = _tokenize_expression(recipe)
    valid = (
        tokenized_recipe[0] == '('
        and tokenized_recipe[-1] == ')'
        and len([pp for pp in tokenized_recipe if pp == '('])
        == len([pp for pp in tokenized_recipe if pp == ')'])
    )
    return valid


def _note(message: str, color: str = None, highlight: str = None):
    """Print a note, possibly in color, possibly highlighting specific text."""
    color = color or ''
    if highlight:
        print(re.sub(f"({highlight})", f"{color}\\g<1>{CLR_OFF}", message))
    else:
        print(f"{color}{message}{CLR_OFF}")


def _fail(message: str, color: str = CLR_ERROR, highlight: str = None):
    _note(message, color, highlight)
    _return_code(2)


def check_containerized_build(files: List[str], recipe: str):
    print(f"Building container for {package_name(recipe)}... 🐳")
    if len([file for file in files if file.endswith('.el')]) > 1:
        main_file = os.path.basename(_main_file(files, recipe))
    else:
        main_file = ''  # no need to specify main file if it's the only file
    output = subprocess.run(
        ['make', 'test', f"PACKAGE_MAIN={main_file}"], stdout=subprocess.PIPE,
    ).stdout
    for line in output.decode().strip().split('\n'):
        # byte-compile-file writes ":Error: ", package-lint ": error: "
        if ':Error: ' in line or ': error: ' in line:
            _fail(line, highlight=r' ?[Ee]rror:')
        elif ':Warning: ' in line or ': warning: ' in line:
            _note(line, CLR_WARN, highlight=r' ?[Ww]arning:')
        elif line.startswith('### '):
            _note(line, CLR_INFO)
        elif not line.startswith('make[1]: Leaving directory'):
            print(line)
    print()


def _files_in_recipe(recipe: str, elisp_dir: str) -> list:
    files = run_build_script(
        f"""
        (require 'package-build)
        (send-string-to-terminal
          (let* ((package-build-working-dir "{os.path.dirname(elisp_dir)}")
                 (rcp {_recipe_struct_elisp(recipe)}))
            (mapconcat (lambda (x) (format "%s" x))
                       (package-build--expand-source-file-list rcp) "\n")))
        """
    ).split('\n')
    return sorted(f for f in files if os.path.exists(os.path.join(elisp_dir, f)))


def _files_in_default_recipe(recipe: str, elisp_dir: str) -> list:
    try:
        return _files_in_recipe(_default_recipe(recipe), elisp_dir)
    except ChildProcessError:
        # It is possible that the default recipe is completely invalid and
        # will throw an error -- in that case, just return the empty list:
        return []


def _set_branch(recipe: str, branch_name: str) -> str:
    """Set the branch on the given recipe.
    >>> _set_branch('(abcdef :fetcher hg :url "a/b")', "feature1")
    '(abcdef :fetcher hg :url "a/b" :branch "feature1")'
    """
    tokens = _tokenize_expression(recipe)
    if ':branch' in tokens:
        index = tokens.index(':branch')
        tokens[index + 1] = branch_name
    else:
        tokens.insert(-1, ':branch')
        tokens.insert(-1, f'"{branch_name}"')
    return '(' + ' '.join(tokens[1:-1]) + ')'


def _default_recipe(recipe: str) -> str:
    """Simplify the given recipe, usually to the default.
    # >>> _default_recipe('(recipe :repo a/b :fetcher hg :branch na :files ("*.el"))')
    # '(recipe :repo a/b :fetcher hg :branch na)'
    >>> _default_recipe('(recipe :fetcher hg :url "a/b")')
    '(recipe :url "a/b" :fetcher hg)'
    """
    tokens = _tokenize_expression(recipe)
    fetcher = tokens.index(':fetcher')
    repo_or_url_token = ':repo' if ':repo' in tokens else ':url'
    repo = tokens.index(repo_or_url_token)
    indices = [1, repo, repo + 1, fetcher, fetcher + 1]
    if ':branch' in tokens:
        branch = tokens.index(':branch')
        indices += [branch, branch + 1]
    return '(' + ' '.join(operator.itemgetter(*indices)(tokens)) + ')'


@functools.lru_cache()
def _tokenize_expression(expression: str) -> List[str]:
    """Turn an elisp expression into a list of tokens.
    >>> _tokenize_expression('(shx :repo "riscy/xyz" :fetcher github) ; comment')
    ['(', 'shx', ':repo', '"riscy/xyz"', ':fetcher', 'github', ')']
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, 'scratch'), 'w') as scratch:
            scratch.write(expression)
        parsed_expression = run_build_script(
            f"""
            (send-string-to-terminal
              (format "%S" (with-temp-buffer (insert-file-contents "{scratch.name}")
                                             (read (current-buffer)))))
            """
        )
    parsed_expression = parsed_expression.replace('(', ' ( ')
    parsed_expression = parsed_expression.replace(')', ' ) ')
    tokenized_expression = parsed_expression.split()
    return tokenized_expression


def package_name(recipe: str) -> str:
    """Return the package's name, based on the recipe.
    >>> package_name('(shx :files ...)')
    'shx'
    """
    return _tokenize_expression(recipe)[1]


def _main_file(files: List[str], recipe: str) -> str:
    """Figure out the 'main' file of the recipe, per MELPA convention.
    >>> _main_file(['pkg/a.el', 'pkg/b.el'], '(a :files ...)')
    'pkg/a.el'
    >>> _main_file(['a.el', 'b.el'], '(b :files ...)')
    'b.el'
    >>> _main_file(['a.el', 'a-pkg.el'], '(a :files ...)')
    'a-pkg.el'
    """
    name = package_name(recipe)
    try:
        return next(
            el
            for el in sorted(files)
            if os.path.basename(el) == f"{name}-pkg.el"
            or os.path.basename(el) == f"{name}.el"
        )
    except StopIteration:
        return ''


def _write_requirements(files: List[str], recipe: str):
    """Create a little elisp script that Docker will run as setup."""
    with open('_requirements.el', 'w') as requirements_el:
        # NOTE: emacs --script <file.el> will set `load-file-name' to <file.el>
        # which can disrupt the compilation of packages that use that variable:
        requirements_el.write('(let ((load-file-name nil))')
        requirements_el.write(
            '''
            (require 'package)
            (package-initialize)
            (setq package-archives nil)
            ;; FIXME: is it still necessary to use GNU elpa mirror?
            (add-to-list 'package-archives '("gnu"   . "http://mirrors.163.com/elpa/gnu/"))
            (add-to-list 'package-archives '("melpa" . "http://melpa.org/packages/"))
            (add-to-list 'package-archives '("org"   . "http://orgmode.org/elpa/"))
            (package-refresh-contents)
            (package-reinstall 'package-lint)
            '''
        )
        for req in requirements(files, recipe):
            if req == 'org':
                # TODO: is there a cleaner way to install a recent version of org?!
                requirements_el.write(
                    "(package-install (cadr (assq 'org package-archive-contents)))"
                )
            elif req != 'emacs':
                # TODO check if we need to reinstall outdated package?
                # e.g. (package-installed-p 'map (version-to-list "2.0"))
                requirements_el.write(f"(package-install '{req})\n")
                if DEBUG:
                    requirements_el.write(f"(require '{req})\n")
        requirements_el.write(') ; end let')


def requirements(
    files: List[str], recipe: str = None, with_versions: bool = False
) -> set:
    reqs = []
    if recipe:
        main_file = _main_file(files, recipe)
        if main_file:
            files = [main_file]
    for filename in (f for f in files if os.path.isfile(f)):
        if filename.endswith('-pkg.el'):
            with open(filename, 'r') as pkg_el:
                reqs.append(_reqs_from_pkg_el(pkg_el))
        elif filename.endswith('.el'):
            with open(filename, 'r') as el_file:
                reqs.append(_reqs_from_el_file(el_file))
    reqs = sum((req.split('(')[1:] for req in reqs), [])
    reqs = [req.replace(')', '').strip().lower() for req in reqs if req.strip()]
    if with_versions:
        return set(reqs)
    for ii, req in enumerate(reqs):
        if '"' not in req:
            _fail(f"Version in '{req}' must be a string!  Attempting patch")
            package, version = reqs[ii].split()
            reqs[ii] = f'{package} "{version}"'
    return {req.split('"')[0].strip() for req in reqs}


def _reqs_from_pkg_el(pkg_el: TextIO) -> str:
    """Pull the requirements out of a -pkg.el file.
    >>> _reqs_from_pkg_el(io.StringIO('''(define-package "x" "1.2" "A pkg." '((emacs "31.5") (xyz "123.4")))'''))
    '( ( emacs "31.5" ) ( xyz "123.4" ) )'
    """
    reqs = pkg_el.read()
    reqs = ' '.join(_tokenize_expression(reqs))
    reqs = reqs[reqs.find('( (') :]
    reqs = reqs[: reqs.find(') )') + 3]
    return reqs


def _reqs_from_el_file(el_file: TextIO) -> str:
    """Hacky function to pull the requirements out of an elisp file.
    >>> _reqs_from_el_file(io.StringIO(';; package-requires: ((emacs "24.4"))'))
    '((emacs "24.4"))'
    """
    for line in el_file.readlines():
        match = re.match('[; ]*Package-Requires:(.*)$', line, re.I)
        if match:
            return match.groups()[0].strip()
    return ''


def _check_license_github(clone_address: str) -> bool:
    """Use the GitHub API to check for a license.
    Return False if unable to check (e.g. it's not on GitHub).
    """
    # TODO: gitlab also has a license API -- support it?
    # e.g. https://gitlab.com/api/v4/users/jagrg/projects ?
    repo_info = repo_info_github(clone_address)
    if not repo_info:
        return False
    license_ = repo_info.get('license')
    if license_ and license_.get('name') in VALID_LICENSES_GITHUB:
        print(f"- GitHub API found `{license_.get('name')}`")
        return True
    if license_:
        _note(f"- GitHub API found `{license_.get('name')}`")
        if license_.get('name') == 'Other':
            _note('  - Try to use a standard format for your license file.', CLR_WARN)
            print('    See: https://github.com/licensee/licensee')
        return True
    _fail('- Add a LICENSE file that GitHub can detect (e.g. no markup) if possible')
    print('  See: https://github.com/licensee/licensee')
    return True


@functools.lru_cache()
def repo_info_github(clone_address: str) -> dict:
    """What does the GitHub API say about the repo?"""
    if clone_address.endswith('.git'):
        clone_address = clone_address[:-4]
    match = re.search(r'github.com/([^"]*)', clone_address, flags=re.I)
    if not match:
        return {}
    response = requests.get(f"{GITHUB_API}/{match.groups()[0].rstrip('/')}")
    if not response.ok:
        return {}
    return dict(response.json())


def _check_repo_for_license(elisp_dir: str) -> bool:
    """Scan any COPYING or LICENSE files."""
    for license_ in glob.glob(os.path.join(elisp_dir, '*')):
        license_ = os.path.basename(license_)
        if license_.startswith('LICENSE') or license_.startswith('COPYING'):
            with open(os.path.join(elisp_dir, license_)) as stream:
                print(f"<!-- {license_} excerpt: `{stream.readline().strip()}...` -->")
            return True
    _fail('- Add a LICENSE or COPYING file to the repository')
    return False


def _check_files_for_license_boilerplate(files: List[str]) -> bool:
    """Check a list of elisp files for license boilerplate."""
    individual_files_licensed = True
    for file in files:
        if not file.endswith('.el') or file.endswith('-pkg.el'):
            continue
        with open(file) as stream:
            license_ = _check_file_for_license_boilerplate(stream)
        basename = os.path.basename(file)
        if not license_:
            _fail(
                '- Add license boilerplate or an [SPDX-License-Identifier]'
                '(https://spdx.org/using-spdx-license-identifier)'
                f" to {basename}"
            )
            individual_files_licensed = False
    return individual_files_licensed


def _check_file_for_license_boilerplate(el_file: TextIO) -> str:
    """Check an elisp file for some license boilerplate.
    >>> _check_file_for_license_boilerplate(io.StringIO('SPDX-License-Identifier:  ISC '))
    'ISC'
    >>> _check_file_for_license_boilerplate(io.StringIO('GNU General Public License'))
    'GPL'
    """
    text = el_file.read()
    match = re.search('SPDX-License-Identifier:[ ]+(.*)', text, flags=re.I)
    if match:
        return match.groups()[0].strip()
    # otherwise, look for fingerprints (consider <https://github.com/emacscollective/elx>)
    fingerprints = [
        ('GPL', r'GNU.* General Public License'),
        ('ISC', r'Permission to use, copy, modify, and/or'),
        ('MIT', r'Permission is hereby granted, free of charge, to any person'),
        ('Unlicense', 'This is free and unencumbered software released into'),
        ('Apache 2.0', 'Licensed under the Apache License, Version 2.0'),
        ('BSD 3-Clause', 'Redistribution and use in source and binary forms'),
    ]
    for license_key, license_text in fingerprints:
        if re.search(license_text, text):
            return license_key
    return ''


def print_packaging(
    files: List[str],
    recipe: str,
    use_default_recipe: bool,
    elisp_dir: str,
    clone_address: str = None,
):
    """Print additional details (how it's licensed, what files, etc.)"""
    _note('### Package ###\n', CLR_INFO)
    if clone_address and repo_info_github(clone_address).get('archived'):
        _fail('- GitHub repository is archived')
    _check_recipe(files, recipe, use_default_recipe)
    _check_license(files, elisp_dir, clone_address)
    _print_package_requires(files, recipe)
    _print_package_files(files)
    print()


def _print_pr_footnotes(clone_address: str, pr_data: dict, recipe: str):
    _note('<!-- ### Footnotes ###', CLR_INFO, highlight='### Footnotes ###')
    repo_info = repo_info_github(clone_address)
    print(
        '```\n' + ' '.join(recipe.split()).replace(' :', '\n  :') + '\n```'
    )  # prettify
    if repo_info:
        if repo_info.get('archived'):
            _fail('- GitHub repository is archived')
        print(f"- Watched: {repo_info.get('watchers_count')}")
        print(f"- Created: {repo_info.get('created_at', '').split('T')[0]}")
        print(f"- Updated: {repo_info.get('updated_at', '').split('T')[0]}")
    print(f"- PR by {pr_data['user']['login']}: {clone_address}")
    if pr_data['user']['login'].lower() not in clone_address.lower():
        _note("- NOTE: Repo and recipe owner don't match", CLR_WARN)
    print('-->\n')


def _check_license(files: List[str], elisp_dir: str, clone_address: str = None):
    repo_licensed = False
    if clone_address and _check_license_github(clone_address):
        return
    if not repo_licensed:
        repo_licensed = _check_repo_for_license(elisp_dir)
    individual_files_licensed = _check_files_for_license_boilerplate(files)
    if not repo_licensed and not individual_files_licensed:
        _fail('- Use a GPL-compatible license.')
        print(
            '  See: https://www.gnu.org/licenses/license-list.en.html#GPLCompatibleLicenses'
        )


def _check_recipe(files: List[str], recipe: str, use_default_recipe: bool):
    if ':branch' in recipe:
        _note('- Avoid specifying `:branch` except in unusual cases', CLR_WARN)
    if 'gitlab' in recipe and (':repo' not in recipe or ':url' in recipe):
        # TODO: recipes that do this are failing much higher in the pipeline
        _fail('- With the GitLab fetcher you MUST set :repo and you MUST NOT set :url')
    if not _main_file(files, recipe):
        _fail(f"- No .el file matches the name '{package_name(recipe)}'")
    if ':files' in recipe and ':defaults' not in recipe:
        _note('- Prefer the default recipe if possible.', CLR_WARN)
        if use_default_recipe:
            _fail(f"  It seems to be equivalent: `{_default_recipe(recipe)}`")


def _print_package_requires(files: List[str], recipe: str):
    """Print the list of Package-Requires from the 'main' file.
    Report on any mismatches between this file and other files, since the ones
    in the other files will be ignored.
    """
    print('- Package-Requires: ', end='')
    main_requirements = requirements(files, recipe, with_versions=True)
    print(', '.join(req for req in main_requirements) if main_requirements else 'n/a')
    for file in files:
        file_requirements = set(requirements([file], with_versions=True))
        if file_requirements and file_requirements > main_requirements:
            _fail(
                f"  - Package-Requires mismatch between {os.path.basename(file)} and "
                f"{os.path.basename(_main_file(files, recipe))}!"
            )


def _print_package_files(files: List[str]):
    for file in files:
        if os.path.isdir(file):
            print(f"- {CLR_ULINE}{file}{CLR_OFF} -- directory")
            continue
        if not file.endswith('.el'):
            print(f"- {CLR_ULINE}{file}{CLR_OFF} -- not elisp")
            continue
        if file.endswith('-pkg.el'):
            _note(f"- {file} -- consider excluding this; MELPA creates one", CLR_WARN)
            continue
        with open(file) as stream:
            try:
                header = stream.readline()
                header = header.split('-*-')[0]
                header = header.split(' --- ')[1]
                header = header.strip()
            except (IndexError, UnicodeDecodeError):
                header = f"{CLR_ERROR}(no header){CLR_OFF}"
                _return_code(2)
            print(
                f"- {CLR_ULINE}{file}{CLR_OFF}"
                f" ({_check_file_for_license_boilerplate(stream) or 'unknown license'})"
                + (f" -- {header}" if header else "")
            )
        if file.endswith('-pkg.el'):
            _note('  - Consider excluding this file; MELPA will create one', CLR_WARN)


def print_similar_packages(package_name: str):
    """Print list of similar, or at least similarly named, packages."""
    keywords = [package_name]
    keywords += [package_name[:-5]] if package_name.endswith('-mode') else []
    keywords += ['org-' + package_name[3:]] if package_name.startswith('ox-') else []
    keywords += ['ox-' + package_name[4:]] if package_name.startswith('org-') else []
    all_candidates = {
        **_known_packages(),
        **_emacswiki_packages(keywords),
    }
    best_candidates = []
    for candidate in all_candidates:
        if any(keyword in candidate for keyword in keywords):
            best_candidates.append(candidate)
    if not best_candidates:
        return
    _note('### Similarly named ###\n', CLR_INFO)
    for name in best_candidates[:10]:
        print(f"- {name}: {all_candidates[name]}")
    if package_name in all_candidates:
        _fail(f"- Error: a package called '{package_name}' exists", highlight='Error:')
    print()


@functools.lru_cache()
def _known_packages() -> dict:
    melpa_packages = {
        package: f"https://melpa.org/#/{package}"
        for package in requests.get('http://melpa.org/archive.json').json()
    }
    epkgs = 'https://raw.githubusercontent.com/emacsmirror/epkgs/master/.gitmodules'
    epkgs_parser = configparser.ConfigParser()
    epkgs_parser.read_string(requests.get(epkgs).text)
    epkgs_packages = {
        epkg.split('"')[1]: 'https://' + data['url'].replace(':', '/')[4:]
        for epkg, data in epkgs_parser.items()
        if epkg != 'DEFAULT'
    }
    return {**epkgs_packages, **melpa_packages}


def _emacswiki_packages(keywords: List[str]) -> dict:
    """Check mirrored emacswiki.org for 'keywords'.
    >>> _emacswiki_packages(keywords=['newpaste'])
    {'newpaste': 'https://github.com/emacsmirror/emacswiki.org/blob/master/newpaste.el'}
    """
    packages = {}
    for keyword in keywords:
        el_file = keyword if keyword.endswith('.el') else (keyword + '.el')
        pkg = f"https://github.com/emacsmirror/emacswiki.org/blob/master/{el_file}"
        if requests.get(pkg).ok:
            packages[keyword] = pkg
    return packages


def yes_p(text: str) -> bool:
    """Ask user a yes/no question."""
    while True:
        keep = input(f"{text} [y/n] ").strip().lower()
        if keep.startswith('y') or keep.startswith('n'):
            break
    return not keep.startswith('n')


def check_melpa_recipe(recipe: str):
    """Check a MELPA recipe definition."""
    _return_code(0)
    with tempfile.TemporaryDirectory() as elisp_dir:
        # package-build prefers the directory to be named after the package:
        elisp_dir = os.path.join(elisp_dir, package_name(recipe))
        clone_address = _clone_address(recipe)
        if _local_repo():
            print(f"Using local repository at {_local_repo()}")
            subprocess.run(['cp', '-r', _local_repo(), elisp_dir])
            _run_checks(recipe, elisp_dir)
        elif _clone(clone_address, elisp_dir, _branch(recipe), _fetcher(recipe)):
            _run_checks(recipe, elisp_dir, clone_address)


def _fetcher(recipe: str) -> str:
    tokenized_recipe = _tokenize_expression(recipe)
    return tokenized_recipe[tokenized_recipe.index(':fetcher') + 1]


def _local_repo() -> str:
    local_repo = os.path.expanduser(os.environ.get('LOCAL_REPO', ''))
    assert not local_repo or os.path.isdir(local_repo)
    return local_repo


def _clone(repo: str, into: str, branch: str, fetcher: str = 'github') -> bool:
    """Try to clone the repository; return whether we succeeded."""
    print(f"Checking out {repo}" + (f" ({branch} branch)" if branch else ""))

    # check if we're being used in GitHub CI -- if so, modify the branch
    if not branch and 'RECIPE' in os.environ:
        branch = (
            os.environ.get('CI_BRANCH', '')
            or os.path.split(os.environ.get('GITHUB_REF', ''))[-1]
            or os.environ.get('TRAVIS_PULL_REQUEST_BRANCH', '')
            or os.environ.get('TRAVIS_BRANCH', '')
        )
        if branch:
            _note(f"CI workflow detected; using branch '{branch}'", CLR_INFO)

    scm = 'hg' if fetcher == 'hg' else 'git'
    if not requests.get(repo).ok:
        _fail(f"Unable to locate {repo}")
        return False
    subprocess.run(['mkdir', '-p', into])
    if scm == 'git':
        # If a package's repository doesn't use the master branch, then the
        # MELPA recipe must specify the branch using the :branch keyword
        # https://github.com/melpa/melpa/pull/6712
        options = ['--branch', branch if branch else 'master']
        options += ['--single-branch']
        if fetcher in {'github', 'gitlab', 'bitbucket'}:
            options += ['--depth', '1']
    elif scm == 'hg':
        options = ['--branch', branch if branch else 'default']
    else:
        _fail(f"Unrecognized SCM: {scm}")
        return False
    git_command = [scm, 'clone', *options, repo, into]
    # git clone prints to stderr, oddly enough:
    result = subprocess.run(git_command, check=True)
    if result.returncode != 0:
        _fail(f"Unable to clone:\n  {' '.join(git_command)}")
        return False
    return True


def _branch(recipe: str) -> str:
    """Return the recipe's branch if available, else the empty string.
    >>> _branch('(shx :branch "develop" ...)')
    'develop'
    >>> _branch('(shx ...)')
    ''
    """
    tokenized_recipe = _tokenize_expression(recipe)
    if ':branch' not in tokenized_recipe:
        return ''
    return tokenized_recipe[tokenized_recipe.index(':branch') + 1].strip('"')


def check_melpa_pr(pr_url: str):
    """Check a PR on MELPA."""
    _return_code(0)
    match = re.search(MELPA_PR, pr_url)  # MELPA_PR's 0th group has the number
    assert match

    pr_data = requests.get(f"{MELPA_PULL_API}/{match.groups()[0]}").json()
    if 'changed_files' not in pr_data:
        _fail(f"{pr_url} does not appear to be a MELPA PR: {pr_data}")
        return
    if int(pr_data['changed_files']) != 1:
        _note('Only add one recipe per pull request', CLR_ERROR)
        return
    filename, recipe = _filename_and_recipe(pr_data['diff_url'])
    if not filename or not recipe:
        _note(f"Unable to build the pull request at {pr_url}", CLR_ERROR)
        return
    if filename != package_name(recipe):
        _fail(f"Recipe filename '{filename}' does not match '{package_name(recipe)}'")
        return

    clone_address: str = _clone_address(recipe)
    with tempfile.TemporaryDirectory() as elisp_dir:
        # package-build prefers the directory to be named after the package:
        elisp_dir = os.path.join(elisp_dir, package_name(recipe))
        if _clone(
            clone_address,
            into=elisp_dir,
            branch=_branch(recipe),
            fetcher=_fetcher(recipe),
        ):
            _run_checks(recipe, elisp_dir, clone_address, pr_data)


@functools.lru_cache()
def _filename_and_recipe(pr_data_diff_url: str) -> Tuple[str, str]:
    """Determine the filename and the contents of the user's recipe."""
    # TODO: use https://developer.github.com/v3/repos/contents/ instead of 'patch'
    diff_text = requests.get(pr_data_diff_url).text
    if (
        'new file mode' not in diff_text
        or 'a/recipes' not in diff_text
        or 'b/recipes' not in diff_text
    ):
        return '', ''
    with tempfile.TemporaryDirectory() as tmpdir:
        with subprocess.Popen(
            ['patch', '-s', '-o', os.path.join(tmpdir, 'patch')], stdin=subprocess.PIPE,
        ) as process:
            assert process.stdin  # pacifies type-checker
            process.stdin.write(diff_text.encode())
        with open(os.path.join(tmpdir, 'patch')) as patch_file:
            basename = diff_text.split('\n')[0].split('/')[-1]
            return basename, patch_file.read().strip()


@functools.lru_cache()
def _clone_address(recipe: str) -> str:
    """Fetch the upstream repository URL for the recipe.
    >>> _clone_address('(shx :repo "riscy/shx-for-emacs" :fetcher github)')
    'https://github.com/riscy/shx-for-emacs.git'
    >>> _clone_address('(pmdm :fetcher hg :url "https://hg.serna.eu/emacs/pmdm")')
    'https://hg.serna.eu/emacs/pmdm'
    """
    return run_build_script(
        f"""
        (require 'package-recipe)
        (send-string-to-terminal
          (package-recipe--upstream-url {_recipe_struct_elisp(recipe)}))
        """
    )


@functools.lru_cache()
def _recipe_struct_elisp(recipe: str) -> str:
    """Turn the recipe into a serialized 'package-recipe' object."""
    name = package_name(recipe)
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, name), 'w') as file:
            file.write(recipe)
        return run_build_script(
            f"""
            (require 'package-recipe)
            (let ((package-build-recipes-dir "{tmpdir}"))
              (send-string-to-terminal (format "%S" (package-recipe-lookup "{name}"))))
            """
        )


@functools.lru_cache()
def run_build_script(script: str) -> str:
    """Run an elisp script in a package-build context.
    >>> run_build_script('(send-string-to-terminal "Hello world")')
    'Hello world'
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        for filename, content in _package_build_files().items():
            with open(os.path.join(tmpdir, filename), 'w') as file:
                file.write(content)
        script = f"""(progn (add-to-list 'load-path "{tmpdir}") {script})"""
        result = subprocess.run(
            ['emacs', '--batch', '--eval', script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise ChildProcessError(result.stderr.decode())
        return result.stdout.decode().strip()


@functools.lru_cache()
def _package_build_files() -> dict:
    """Grab the required package-build files from the MELPA repo."""
    return {
        filename: requests.get(
            'https://raw.githubusercontent.com/melpa/melpa/master/'
            f'package-build/{filename}'
        ).text
        for filename in [
            'package-build-badges.el',
            'package-build.el',
            'package-recipe-mode.el',
            'package-recipe.el',
        ]
    }


@functools.lru_cache()
def _package_recipe_el() -> str:
    """Grab the source code for MELPA's package-build/package-recipe.el"""
    return requests.get(
        'https://raw.githubusercontent.com/melpa/melpa/master/'
        'package-build/package-recipe.el'
    ).text


def _check_melpa_pr_loop() -> None:
    """Check MELPA pull requests in a loop."""
    for pr_url in _fetch_pull_requests():
        print(f"Found MELPA PR {pr_url}")
        check_melpa_pr(pr_url)
        if _return_code() != 0:
            _fail('<!-- This PR failed -->')
        else:
            _note('<!-- This PR passed -->')
        print('-' * 79)


def _fetch_pull_requests() -> Iterator[str]:
    """Repeatedly yield PR URL's."""
    # TODO: only supports macOS (needs pbpaste or equivalents)
    previous_pr_url = None
    while True:
        while True:
            match = re.search(MELPA_PR, subprocess.check_output('pbpaste').decode())
            pr_url = match.string[: match.end()] if match else None
            if match and pr_url and pr_url != previous_pr_url:
                break
            print('Watching clipboard for MELPA PR... ', end='\r')
            time.sleep(1)
        previous_pr_url = pr_url
        yield pr_url


if __name__ == '__main__':
    if 'MELPA_PR_URL' in os.environ:
        check_melpa_pr(os.environ['MELPA_PR_URL'])
        sys.exit(_return_code())
    elif 'RECIPE' in os.environ:
        check_melpa_recipe(os.environ['RECIPE'])
        sys.exit(_return_code())
    elif 'RECIPE_FILE' in os.environ:
        with open(os.environ['RECIPE_FILE'], 'r') as file:
            check_melpa_recipe(file.read())
        sys.exit(_return_code())
    else:
        _check_melpa_pr_loop()
