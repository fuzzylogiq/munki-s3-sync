#!/usr/bin/env python3

"""Automated catalog promotion for Munki pkgsinfo files.

Promotes packages between catalogs based on the _autopromotion_catalogs key
in each pkgsinfo plist. Each key in _autopromotion_catalogs is a number of days
after _metadata.creation_date; when that time has elapsed, the package's catalogs
are updated to the corresponding value.

Example pkgsinfo key:
    <key>_autopromotion_catalogs</key>
    <dict>
        <key>3</key>
        <array>
            <string>production</string>
        </array>
    </dict>

This would promote the package to "production" 3 days after import.
"""

import plistlib
import os
import subprocess
from datetime import datetime, timedelta

GIT = "/usr/bin/git"
GITHUB_CLI = "gh"
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
REPO_DIR = os.path.join(os.environ.get('GITHUB_WORKSPACE', os.getcwd()), "munki_repo")

class Error(Exception):
    """Base class for domain-specific exceptions."""


class GitError(Error):
    """Git exceptions."""


class BranchError(Error):
    """Branch-related exceptions."""


# Utility functions
def run_cmd(cmd):
    """Run a command and return the output."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    (out, err) = proc.communicate()
    results_dict = {
        'stdout': out,
        'stderr': err,
        'status': proc.returncode,
        'success': proc.returncode == 0
    }
    return results_dict


# Git-related functions
def git_run(arglist):
    """Run git with the argument list."""
    owd = os.getcwd()
    os.chdir(REPO_DIR)
    gitcmd = [GIT]
    for arg in arglist:
        gitcmd.append(str(arg))
    results = run_cmd(gitcmd)
    os.chdir(owd)
    if not results['success']:
        raise GitError("Git error: %s" % (results['stderr'].decode('utf-8')))
    return results['stdout']


def branch_list():
    """Get the list of current git branches."""
    git_args = ['branch']
    branch_output = git_run(git_args).rstrip()
    if branch_output:
        return [x.strip().strip('* ')
                for x in branch_output.decode().split('\n')]
    return []


def current_branch():
    """Return the name of the current git branch."""
    git_args = ['symbolic-ref', '--short', 'HEAD']
    return git_run(git_args).decode().strip()


def change_feature_branch(branch, new=False):
    """Swap to feature branch."""
    gitcmd = ['checkout']
    if new:
        gitcmd.append('-b')
    gitcmd.append(branch)
    try:
        git_run(gitcmd)
    except GitError as e:
        raise BranchError(
            "Couldn't switch to '%s': %s" % (branch, e)
        )


def git_push(branch):
    """Perform a git push."""
    print('Running `git push`...')
    gitpushcmd = ['push', '--no-verify', '--set-upstream', 'origin']
    gitpushcmd.append(branch)
    try:
        print(git_run(gitpushcmd))
    except GitError as e:
        print("Failed to push branch %s" % branch)
        return {
            'success': False,
            'error': e,
            'branch': branch
        }
    return {
        'success': True
    }


def pull_request(branchname):
    """Create Pull request using the gh cli tool."""
    if not GITHUB_TOKEN:
        print('Pull request not created.. GITHUB_TOKEN not set')
        return
    print('Creating Pull Request...')
    run_cmd([
      GITHUB_CLI,
      "pr", "create",
      "-B", "main",
      "-H", branchname,
      "-t", branchname,
      "-f"
    ])


def create_commit(pkginfo_path, name, version, catalogs):
    """Create git commit."""
    gitaddcmd = ['add']
    gitaddcmd.append(pkginfo_path)
    git_run(gitaddcmd)
    print('Creating commit...')
    gitcommitcmd = ['commit', '-m']
    message = "%s: promoting %s to %s" % (name,
                                       version,
                                       ', '.join(catalogs))
    gitcommitcmd.append(message)
    git_run(gitcommitcmd)


# Pkginfo helpers
def read_pkginfo(pkginfo_path):
    with open(pkginfo_path, 'rb') as f:
        pkginfo = plistlib.load(f)
    return pkginfo or None

def write_pkginfo(pkginfo_path, pkginfo):
    with open(pkginfo_path, 'wb') as f:
        plistlib.dump(pkginfo, f)

def promote(pkginfo_path):
    pkginfo = read_pkginfo(pkginfo_path)
    if pkginfo:
        autopromotion_catalogs = pkginfo.get("_autopromotion_catalogs")
        if autopromotion_catalogs is None:
            print(f"No autopromotion_catalogs in pkginfo")
            return False
        metadata = pkginfo.get("_metadata")
        if metadata is None:
            print(f"No _metadata in pkginfo")
            return False
        creation_date = metadata.get("creation_date")
        if creation_date is None:
            print(f"No creation_date in pkginfo")
            return False
        current_catalogs = pkginfo.get("catalogs")
        now = datetime.now(tz=creation_date.tzinfo)
        for days, catalogs in autopromotion_catalogs.items():
            if current_catalogs == catalogs:
                print("Already promoted")
                return False
            promo_date = creation_date + timedelta(days=int(days))
            if now > promo_date:
                print(f"Autopromoting to {catalogs} as past {days} day(s)")
                pkginfo["catalogs"] = catalogs
                write_pkginfo(pkginfo_path, pkginfo)
                create_commit(pkginfo_path, pkginfo["name"], pkginfo["version"], pkginfo["catalogs"])
        return True
    return False

def main():
    git_errors = []
    if current_branch() != "main":
        change_feature_branch("main")
    date = datetime.today().strftime("%Y-%m-%d")
    branchname = f"autopromotion-{date}"
    change_feature_branch(branchname, new=True)
    all_promoted = []
    for root, dirs, files in os.walk(os.path.join(REPO_DIR, "pkgsinfo")):
        for file in files:
            if file.endswith(".plist"):
                pkginfo_path = os.path.join(root, file)
                promoted = promote(pkginfo_path)
                if promoted:
                    all_promoted.append(pkginfo_path)
                    print(f'{pkginfo_path} was promoted!')
                else:
                    print(f'{pkginfo_path} was not promoted!')
    # Check if there are actual commits on the branch before pushing
    has_commits = run_cmd([GIT, '-C', REPO_DIR, 'log', 'main..' + branchname, '--oneline'])
    if has_commits['stdout'].strip():
        push_result = git_push(branchname)
        if not push_result["success"]:
            git_errors.append(push_result)
        pr_result = pull_request(branchname)
        if pr_result:
            git_errors.append(pr_result)
    else:
        print('No promotions to push, skipping branch push and PR creation')

    print(git_errors)

if __name__ == "__main__":
    main()
