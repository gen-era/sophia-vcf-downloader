import ssl
import sys
import hashlib
import subprocess
import urllib.request
import os
import time

def get_remote_url():
    """Get the remote URL based on environment context."""
    env_context = os.getenv('ENV_CONTEXT', 'prod').lower() 
    if env_context == 'preverif':
        return "https://preverif.sophiagenetics.com/direct/sg/uploaderv2"
    elif env_context == 'vandv':
        return "https://vandv.sophiagenetics.com/direct/sg/uploaderv2"
    return "https://ddm.sophiagenetics.com/direct/sg/uploaderv2"

REMOTE_URL = get_remote_url()
UPLOADER_FILENAME = "sg-upload-v2-latest.jar"
UPLOAD_CHECKSUM_FILENAME = "sg-upload-v2-latest.jar.md5"
VERSION = "1.0.6"

# In case of "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify error please uncomment the following line
# see https://support.sectigo.com/articles/Knowledge/Sectigo-AddTrust-External-CA-Root-Expiring-May-30-2020
ssl._create_default_https_context = ssl._create_unverified_context


def get_remote_checksum():
    """Fetch the checksum of the remote file."""
    url = f"{REMOTE_URL}/{UPLOAD_CHECKSUM_FILENAME}"
    try:
        response = urllib.request.urlopen(url)
        if response.status != 200:
            print(f"Error: Could not find the remote version at {url}")
            sys.exit(1)
        md5sum = response.read().decode('utf-8')
        return md5sum
    except Exception as err:
        print(f"Error: There was a problem connecting to {url}")
        print("If you encounter an [SSL: CERTIFICATE_VERIFY_FAILED] error, please uncomment the following line:")
        print("# ssl._create_default_https_context = ssl._create_unverified_context")
        print(
            "For more information, see https://support.sectigo.com/articles/Knowledge/Sectigo-AddTrust-External-CA-Root-Expiring-May-30-2020")
        print(f"Technical details: {err}")
        return ""


def get_current_checksum():
    """Calculate the checksum of the current file."""
    hash_md5 = hashlib.md5()
    try:
        with open(UPLOADER_FILENAME, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except FileNotFoundError:
        return ""  # return empty checksum if no file found


def download_latest_uploader(retries=3, backoff_factor=2, timeout=10):
    """Download the latest version of the uploader with retries."""
    update_url = f"{REMOTE_URL}/{UPLOADER_FILENAME}"

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(update_url, timeout=timeout) as response:
                with open(UPLOADER_FILENAME, 'wb') as f:
                    f.write(response.read())
            return

        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if attempt == retries:
                break

            sleep_time = backoff_factor ** (attempt - 1)
            time.sleep(sleep_time)


def build_command(args: list):
    """Build the command to run the uploader."""
    command = ['java']
    java_opts = []
    other_args = []
    skip_next = False
    
    # Process arguments, handling --jvm-opts flag
    for i, arg in enumerate(args):
        if arg == sys.argv[0]:
            continue  # Skip script name
        if skip_next:
            skip_next = False
            continue  # Skip the value after --jvm-opts (already processed)
        
        if arg == '--jvm-opts':
            # Get the next argument which contains the JVM options string
            if i + 1 < len(args):
                jvm_opts_string = args[i + 1]
                # Split the string by spaces to get individual options
                jvm_opts_list = jvm_opts_string.split()
                java_opts.extend(jvm_opts_list)
                skip_next = True  # Skip the next arg (the jvm-opts value)
            continue
        
        # Extract JVM options (-X, -XX:) and system properties (-D)
        # These must come before -jar
        if arg.startswith("-X") or arg.startswith("-XX:") or arg.startswith("-D"):
            java_opts.append(arg)
        else:
            # Extract application arguments (everything else)
            other_args.append(arg)
    
    command += java_opts + ['-jar', UPLOADER_FILENAME] + other_args
    return command


def main():
    """Main function to run the script."""
    remote_checksum = get_remote_checksum()

    if remote_checksum == "":
        print("WARN. No new version found. Using previous one!")
    else:
        current_checksum = get_current_checksum()
        if current_checksum == "":
            print(f"Downloading latest uploader version. Checksum: {remote_checksum}.")
            download_latest_uploader()
        else:
            remote_checksum = remote_checksum.strip()
            current_checksum = current_checksum.strip()
            if remote_checksum != current_checksum:
                print(f"Current checksum: {current_checksum}")
                print(f"Remote checksum: {remote_checksum}")
                download_latest_uploader()
                print(f"Updated to version {remote_checksum}.")
            else:
                print(f"Script is up-to-date (checksum {remote_checksum})")

    cmd = build_command(sys.argv)

    # Run the command and capture the return code
    completed_process = subprocess.run(cmd)
    return_code = completed_process.returncode

    # Exit with the same return code as the command
    sys.exit(return_code)


if __name__ == "__main__":
    main()
