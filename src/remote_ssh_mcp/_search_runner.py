"""Self-contained bounded search helper executed on the remote host."""

import base64
import fnmatch
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile


def _emit_limited_process(command, limit, success_codes):
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        lines = []
        terminated_for_limit = False
        try:
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                lines.append(line)
                if len(lines) >= limit:
                    if process.poll() is None:
                        process.terminate()
                        terminated_for_limit = True
                    break
            try:
                return_code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait()
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise

        sys.stdout.buffer.writelines(lines)
        if terminated_for_limit or return_code in success_codes:
            return 0

        errors.seek(0)
        sys.stdout.buffer.write(errors.read())
        return return_code or 1


def _grep(request):
    pattern = request["pattern"]
    path = request["path"]
    glob = request.get("glob")
    case_insensitive = request["case_insensitive"]
    limit = request["limit"]
    ripgrep = shutil.which("rg")

    if ripgrep:
        command = [ripgrep, "-n", "--color=never"]
        if case_insensitive:
            command.append("-i")
        if glob is not None:
            command.extend(["-g", glob])
        command.extend(["--", pattern, path])
    else:
        command = ["grep", "-rnE"]
        if case_insensitive:
            command.append("-i")
        if glob is not None:
            command.append("--include=" + glob)
        command.extend(["--", pattern, path])

    return _emit_limited_process(command, limit, {0, 1})


def _glob(request):
    root = request["path"]
    pattern = request["pattern"]
    limit = request["limit"]
    matches = []

    entry = os.lstat(root)
    if stat.S_ISREG(entry.st_mode):
        if fnmatch.fnmatch(os.path.basename(root), pattern):
            matches.append(root)
    elif stat.S_ISDIR(entry.st_mode):

        def raise_walk_error(error):
            raise error

        for directory, directories, names in os.walk(root, onerror=raise_walk_error):
            directories.sort()
            names.sort()
            for name in names:
                candidate = os.path.join(directory, name)
                if not fnmatch.fnmatch(name, pattern):
                    continue
                if stat.S_ISREG(os.lstat(candidate).st_mode):
                    matches.append(candidate)
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break

    for match in matches:
        print(match)
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    request = json.loads(base64.urlsafe_b64decode(args[0]))
    try:
        if request["operation"] == "grep":
            return _grep(request)
        if request["operation"] == "glob":
            return _glob(request)
        raise ValueError("unknown search operation")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
