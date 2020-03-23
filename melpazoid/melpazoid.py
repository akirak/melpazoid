import configparser
    print_related_packages(recipe)
    """Use the GitHub API to check for a license."""
        ('Apache 2.0', 'Licensed under the Apache License, Version 2.0',),
    package_name = _package_name(recipe)
    shorter_name = package_name[:-5] if package_name.endswith('-mode') else package_name
    known_packages = _known_packages()
    known_names = [name for name in known_packages if shorter_name in name]
    if not known_names:
        return
    _note('\n### Similarly named packages ###\n', CLR_INFO)
    for name in known_names[:10]:
        print(f"- {name} {known_packages[name]}")
    if package_name in known_packages:
        _fail(f"- {package_name} {known_packages[package_name]} is in direct conflict")
def _known_packages() -> dict:
    melpa_packages = {
        package: f"https://melpa.org/#/{package}"
    epkgs = 'https://raw.githubusercontent.com/emacsmirror/epkgs/master/.gitmodules'
    epkgs_parser = configparser.ConfigParser()
    epkgs_parser.read_string(requests.get(epkgs).text)
    epkgs_packages = {
        epkg.split('"')[1]: epkgs_parser[epkg]['url']
        for epkg in epkgs_parser
        if epkg != 'DEFAULT'
    }
    return {**epkgs_packages, **melpa_packages}
        if _clone(clone_address, into=elisp_dir, branch=_branch(recipe), scm=scm):
def _clone(repo: str, into: str, branch: str = None, scm: str = 'git') -> bool:
    print(f"Checking out {repo}")
        _fail(f"Unable to locate {repo}")
    if scm == 'git':
        # MELPA recipe must specify the branch using the :branch keyword
        options = ['--branch', branch if branch else 'master']
        options += ['--depth', '1', '--single-branch']
        options = ['--branch', branch] if branch else []
    git_command = [scm, 'clone', *options, repo, into]
        if _clone(clone_address, into=elisp_dir, branch=_branch(recipe)):