#!/usr/bin/env python3
'''Utility to manage installer/uninstaller files in a munki repo. Requires
a standard munki repo layout of "packages" being in the pkgs folder, and
pkginfo files being in the "pkgsinfo" folder.

Files are stored in S3 using content-addressable storage: each file is keyed
by its SHA-256 hash and sharded into subdirectories (first char / next two chars)
to avoid flat-directory performance issues.'''

import argparse
import base64
import hashlib
import os
import plistlib
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
import urllib3
from botocore.exceptions import ClientError, NoCredentialsError
from progress import ScanProgress, TransferProgress

# Thread-local storage for boto3 sessions/clients.
# boto3 sessions are not thread-safe, so each thread gets its own.
_thread_local = threading.local()

def _get_s3_client():
    '''Returns a thread-local S3 client, creating one if needed.'''
    if not hasattr(_thread_local, 's3_client'):
        session = boto3.session.Session()
        _thread_local.s3_client = session.client('s3')
    return _thread_local.s3_client

def validate_aws(bucket):
    '''Validates that there is an active AWS session to use, and that it has read/write
    permissions on the specified bucket.
    Parameters:
        bucket: name of the S3 storage bucket for munki files
    Returns:
        bool, True if all checks pass.'''
    try:
        client = boto3.client('s3')
        response = client.head_bucket(Bucket=bucket)
        if response['ResponseMetadata']['HTTPStatusCode'] == 200:
            return True
    except NoCredentialsError:
        print('Error: no AWS credentials were found.')
    except ClientError as err:
        print(f'Error: {err}')
    except:
        # Most often, hitting this means SSO session has expired
        print('Error: Generic Exception - check your SSO session is valid?')
    return False

def trigger_sso():
    '''Shells out to trigger an AWS sign-in.'''
    print('Attempting to generate SSO session - your browser should launch shortly.')
    cmd = ['aws', 'sso', 'login']
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as run:
        run.communicate()

def get_files(repo_path, dir_type):
    '''Generates a list of file paths within a repo subdirectory.
    Parameters:
        repo_path: absolute path to the munki repo to process
        dir_type: specify either the pkgs or pkgsinfo directory to be scanned
    Returns:
        files: a list of absolute paths to files'''
    files = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(repo_path, dir_type), followlinks=True):
        for dirname in dirnames:
            if dirname.startswith('.'):
                dirnames.remove(dirname)

        for filename in filenames:
            if filename.startswith('.'):
                continue

            filepath = os.path.join(dirpath, filename)
            files.append(filepath)

    return files


def get_file_hashes(filename, verbose):
    '''Generates SHA-256 hashes for the passed file.
    Parameters:
        filename: the absolute path to a file
    Returns:
        hashes: a dict with the following data:
            hexdigest: hex digest of the SHA-256 file hash (for matching against pkginfos)
            base64: base64 of SHA-256 file hash (for matching against single-part S3 uploads)
            mpbase64: base64 or concat'd sums of chunks (for matching against multi-part uploads)'''
    if not os.path.isfile(filename):
        return None

    hash_function = hashlib.sha256()
    with open(filename, 'rb') as f:
        while 1:
            chunk = f.read(2**16)
            if not chunk:
                break
            hash_function.update(chunk)

    hashes = {
        'hexdigest': hash_function.hexdigest(),
        'base64': base64.b64encode(hash_function.digest()).decode(),
        'mpbase64': ''
    }
    if verbose:
        print(f"  Local file hash: {hashes['hexdigest']}")
        print(f"  Base64 of hash: {hashes['base64']}")

    return hashes

def read_pkginfo(pkginfo_path, repo_path):
    '''Reads a pkginfo plist and extracts file references for sync operations.
    Parameters:
        pkginfo_path: the absolute path to a pkginfo file
        repo_path: the absolute path to the munki repo
    Returns:
       files_data: a dict with the following data:
            pname: the pkginfo display name
            pversion: the version key of the pkginfo
            files: list of dicts, each with name, path, hash, size'''
    try:
        with open(pkginfo_path, 'rb') as f:
            pkgsinfo_data = plistlib.load(f)
    except plistlib.InvalidFileException:
        print(f'{pkginfo_path} is not a valid plist file')
        return
    except IOError as err:
        print(f'Error reading pkginfo {pkginfo_path}: {err}')
        return

    files_data = {
        'pname': pkgsinfo_data.get('display_name'),
        'pversion': pkgsinfo_data.get('version'),
        'files': []
    }
    file_keys = [
        {'location': 'installer_item_location',
         'hashstring': 'installer_item_hash',
         'size': 'installer_item_size'},
        {'location': 'uninstaller_item_location',
         'hashstring': 'uninstaller_item_hash',
         'size': 'uninstaller_item_size'}
    ]
    for key_type in file_keys:
        if pkgsinfo_data.get(key_type['location']):
            fname = os.path.basename(pkgsinfo_data[key_type['location']])
            fpath = os.path.join(repo_path, 'pkgs', pkgsinfo_data[key_type['location']])
            fhash = pkgsinfo_data[key_type['hashstring']]
            fsize = pkgsinfo_data[key_type['size']]
            files_data['files'].append({'name': fname, 'path': fpath, 'hash': fhash, 'size': fsize})

    return files_data

def construct_local_dirs(file_path):
    '''Creates the directory structure for the given local file path.
    Parameters:
        file_path: a string with the absolute path to the file in the munki repo'''
    try:
        os.makedirs(os.path.dirname(file_path))
    except FileExistsError:
        pass


def upload_file(item, bucket, callback=None):
    '''Uploads a given file to S3 storage using content-addressable path.
    Parameters:
        item: a dict containing at least: path, hash
        bucket: the S3 bucket to upload the file into
        callback: optional callable(bytes_transferred) for progress tracking'''
    key_path = item['hash'][0] + '/' + item['hash'][1:3] + '/' + item['hash']
    client = _get_s3_client()
    extra_args = {'ChecksumAlgorithm': 'SHA256'}
    client.upload_file(item['path'], bucket, key_path, ExtraArgs=extra_args,
                       Callback=callback)


def download_file(item, bucket, callback=None):
    '''Downloads a given file from S3 storage using content-addressable path.
    Parameters:
        item: a dict containing at least: path, hash
        bucket: the S3 bucket to download the file from
        callback: optional callable(bytes_transferred) for progress tracking'''
    key_path = item['hash'][0] + '/' + item['hash'][1:3] + '/' + item['hash']
    client = _get_s3_client()
    try:
        client.download_file(Bucket=bucket, Key=key_path, Filename=item['path'],
                             Callback=callback)
    except urllib3.exceptions.IncompleteRead:
        raise
    except ClientError:
        raise


def verify_s3_file(item, bucket):
    '''Verifies if an object exists on S3 and whether its SHA-256 hash matches.
    Parameters:
        item: a dict containing at least: path, hash, hashes
        bucket: the S3 bucket to check
    Returns:
        verified: bool, True if the file exists and hash matches.'''
    key_path = item['hash'][0] + '/' + item['hash'][1:3] + '/' + item['hash']
    client = _get_s3_client()
    try:
        result = client.get_object_attributes(
                 Bucket=bucket, Key=key_path, ObjectAttributes=['Checksum', 'ObjectParts'])
        if 'ObjectParts' in result:
            # TODO: verify multi-part upload checksums
            pass
        else:
            if item['hashes']['base64'] != result['Checksum']['ChecksumSHA256']:
                print('  Error: uploaded hash and local hash do not match!')
                return False
        return True
    except client.exceptions.NoSuchKey:
        return False

def _scan_for_upload(pkginfo_path, repo, bucket, verbose, ignore, progress=None):
    '''Phase 1 worker: reads a pkginfo and determines which files need uploading.'''
    fdata = read_pkginfo(pkginfo_path, repo)
    if not fdata:
        return None, [], []
    if progress:
        progress.set_current(f"{fdata['pname']} {fdata['pversion']}")
    to_upload = []
    already_uploaded = []
    for item in fdata['files']:
        item['hashes'] = get_file_hashes(item['path'], verbose)
        if item['hashes'] is None and ignore:
            print(f"  {fdata['pname']}: item not found...ignoring as --ignore/-i is set...")
            continue
        if item['hashes'] is None:
            print(f"  {fdata['pname']}: file not found at {item['path']}")
            continue
        if item['hashes']['hexdigest'] != item['hash']:
            print(f"  {fdata['pname']}: actual hash and pkginfo hash do not match!")
        if verify_s3_file(item, bucket):
            already_uploaded.append(item)
        else:
            to_upload.append(item)
    return fdata, to_upload, already_uploaded


def _upload_item(item, bucket, progress=None):
    '''Phase 2 worker: uploads a single file to S3.'''
    callback = progress.file_callback(item) if progress else None
    try:
        upload_file(item, bucket, callback=callback)
        if progress:
            progress.file_done(item)
        return item, True
    except Exception as e:
        if progress:
            progress.file_error(item, str(e))
        else:
            print(f'  Error uploading {item["name"]}: {e}')
        return item, False


def process_uploads(repo, bucket, verbose, ignore, files=None):
    '''Uploads any local files that are not present in the S3 bucket.
    Uses a two-phase approach: phase 1 scans pkgsinfos to determine what
    needs uploading (including S3 presence check), phase 2 concurrently uploads.
    Arguments:
        repo: absolute path to the munki repo
        bucket: name of the S3 storage bucket'''
    pkgsinfos = files if files else get_files(repo, 'pkgsinfo')

    # Phase 1: scan to determine what needs uploading
    all_to_upload = []
    with ScanProgress(total=len(pkgsinfos), label="Scanning pkgsinfos for uploads") as sp:
        try:
            for pkginfo in pkgsinfos:
                fdata, to_upload, already_uploaded = _scan_for_upload(
                    pkginfo, repo, bucket, verbose, ignore, progress=sp)
                sp.advance()
                if not fdata:
                    continue
                all_to_upload.extend(to_upload)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return

    if not all_to_upload:
        print("All packages already in S3.")
        return

    # Phase 2: concurrent upload
    uploaded_files = 0
    failed_files = 0
    uploaded_file_size = 0
    transfer_workers = min(8, len(all_to_upload))
    with TransferProgress(all_to_upload, mode='upload') as tp:
        with ThreadPoolExecutor(max_workers=transfer_workers) as transfer_pool:
            futures = {
                transfer_pool.submit(_upload_item, item, bucket, tp): item
                for item in all_to_upload
            }
            try:
                for future in as_completed(futures):
                    item, success = future.result()
                    if success:
                        uploaded_files += 1
                        uploaded_file_size += item['size']
                    else:
                        failed_files += 1
            except KeyboardInterrupt:
                for f in futures:
                    f.cancel()
                transfer_pool.shutdown(wait=False, cancel_futures=True)
                print("\nInterrupted - cancelled pending uploads.")
                return

    summary = f"Uploaded {uploaded_files} file(s) / {int(uploaded_file_size/1024)}MB"
    if failed_files:
        summary += f" ({failed_files} failed)"
    print(summary)

def _scan_for_download(pkginfo_path, repo, verbose, progress=None):
    '''Phase 1 worker: reads a pkginfo and determines which files need downloading.'''
    fdata = read_pkginfo(pkginfo_path, repo)
    if not fdata:
        return None, [], []
    if progress:
        progress.set_current(f"{fdata['pname']} {fdata['pversion']}")
    to_download = []
    already_have = []
    for item in fdata['files']:
        if not os.path.isfile(item['path']):
            to_download.append(item)
        else:
            lfhash = get_file_hashes(item['path'], verbose)
            if lfhash and lfhash['hexdigest'] == item['hash']:
                already_have.append(item)
            else:
                to_download.append(item)
    return fdata, to_download, already_have


def _download_item(item, bucket, progress=None, max_retries=3):
    '''Phase 2 worker: downloads a single file from S3 with retries.'''
    construct_local_dirs(item['path'])
    for attempt in range(1, max_retries + 1):
        callback = progress.file_callback(item) if progress else None
        try:
            download_file(item, bucket, callback=callback)
            if progress:
                progress.file_done(item)
            return item, True
        except Exception as e:
            if os.path.exists(item['path']):
                os.remove(item['path'])
            if attempt < max_retries:
                if progress:
                    progress.file_retry(item, attempt, max_retries)
                else:
                    print(f'  Retry {attempt}/{max_retries} for {item["name"]}: {e}')
                time.sleep(2 ** attempt)
            else:
                if progress:
                    progress.file_error(item, str(e))
                else:
                    print(f'  Error downloading {item["name"]}: {e}')
                return item, False


def process_downloads(repo, bucket, verbose, files=None):
    '''Downloads files from S3 that are referenced in a pkginfo but not present locally.
    Uses a two-phase approach: phase 1 scans pkgsinfos to determine what
    needs downloading, phase 2 concurrently downloads.
    Arguments:
        repo: absolute path to the munki repo
        bucket: name of the S3 storage bucket'''
    pkgsinfos = files if files else get_files(repo, 'pkgsinfo')

    # Phase 1: scan to determine what needs downloading
    all_to_download = []
    with ScanProgress(total=len(pkgsinfos), label="Scanning pkgsinfos") as sp:
        try:
            for pkginfo in pkgsinfos:
                fdata, to_download, already_have = _scan_for_download(
                    pkginfo, repo, verbose, progress=sp)
                sp.advance()
                if not fdata:
                    continue
                all_to_download.extend(to_download)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return

    if not all_to_download:
        print("All packages up to date.")
        return

    # Phase 2: concurrent download
    downloaded_files = 0
    failed_files = 0
    downloaded_file_size = 0
    transfer_workers = min(8, len(all_to_download))
    with TransferProgress(all_to_download, mode='download') as tp:
        with ThreadPoolExecutor(max_workers=transfer_workers) as transfer_pool:
            futures = {
                transfer_pool.submit(_download_item, item, bucket, tp): item
                for item in all_to_download
            }
            try:
                for future in as_completed(futures):
                    item, success = future.result()
                    if success:
                        downloaded_files += 1
                        downloaded_file_size += item['size']
                    else:
                        failed_files += 1
            except KeyboardInterrupt:
                for f in futures:
                    f.cancel()
                transfer_pool.shutdown(wait=False, cancel_futures=True)
                print("\nInterrupted - cancelled pending downloads.")
                return

    summary = f"Downloaded {downloaded_files} file(s) / {int(downloaded_file_size/1024)}MB"
    if failed_files:
        summary += f" ({failed_files} failed)"
    print(summary)

def process_prune(repo, verbose):
    '''Removes local files that are unreferenced by a pkginfo.
    Arguments:
        repo: absolute path to the munki repo'''
    if verbose:
        print('Running file prune to remove files not mentioned in a pkginfo...')
    pkgsinfos = get_files(repo, 'pkgsinfo')
    files_to_keep = []
    for pkginfo in pkgsinfos:
        fdata = read_pkginfo(pkginfo, repo)
        if not fdata:
            continue
        for item in fdata['files']:
            files_to_keep.append(item['path'])
    all_files = get_files(repo, 'pkgs')

    files_to_delete = [x for x in all_files if x not in files_to_keep]
    if verbose:
        print(f'Found {len(files_to_delete)} file(s) to be removed.')
    for item in files_to_delete:
        try:
            os.remove(item)
            if verbose:
                print(f'  Removed {item}')
        except OSError as err:
            print(f'  Error removing {item}: {err}')
    if verbose:
        print('Prune complete!')

def main():
    '''The sync utility abstracts away the upload and download of "pkgs" files in a munki
    repo. It has three different modes:
        upload: processes pkginfo files and looks for files to be uploaded. Uploaded files
                are stored in a specified S3 bucket by SHA-256 hash to avoid name
                collision issues, and sharded in directories by hash.
        download: processes pkginfo files and looks for files that are not on the local
                  system. Uses the SHA-256 hash from the pkginfo to construct the path
                  and download the file from the specified S3 bucket.
        prune: processes pkginfo files and files in the pkgs directory and removes
               files that are not referenced by a pkginfo. In this way, upstream
               changes that remove old installers can be synced to remove cruft
               and keep disk usage under control.'''
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', help='Use more verbose output', action='store_true')
    parser.add_argument('-i', '--ignore', help='ignore pkgs not found on disk', action='store_true')
    parser.add_argument('-f', '--file', help='specify the path to a pkginfo to sync its associated pkg', action='append')
    required_args = parser.add_argument_group('required named arguments')
    required_args.add_argument('-m', '--mode', type=str, choices=['upload', 'download', 'prune'],
                        help='specify the operating mode for file transfer', required=True)
    required_args.add_argument('-r', '--repo', type=str, help='absolute path to the munki repo',
                               required=True)
    parser.add_argument('-b', '--bucket', type=str, help='name of the S3 bucket')
    args = parser.parse_args()
    if '~' in args.repo:
        args.repo = os.path.expanduser(args.repo)

    if args.mode in ('upload', 'download') and not args.bucket:
        parser.error('-b/--bucket is required for upload and download modes')

    match args.mode:
        case 'upload':
            while not validate_aws(args.bucket):
                trigger_sso()
                time.sleep(5)
            process_uploads(args.repo, args.bucket, args.verbose, args.ignore, args.file)

        case 'download':
            while not validate_aws(args.bucket):
                trigger_sso()
                time.sleep(5)
            process_downloads(args.repo, args.bucket, args.verbose, args.file)

        case 'prune':
            process_prune(args.repo, args.verbose)

if __name__ == '__main__':
    main()
