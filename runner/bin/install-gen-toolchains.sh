#!/bin/bash
# Compilers for the languages pymavlink generates, so a review of a generator
# change can run the generated code instead of reading it.
#
# pymavlink's own CI generates but never compiles, and neither did this box:
# eight generator paths were read-verified only. These are the ones apt can
# supply. Swift and Spin2 are not packaged for Ubuntu and are left to
# install-gen-toolchains-manual.md.
#
# Idempotent: safe to re-run, and does nothing when everything is present.
set -u

PKGS=(
    default-jdk         # javac, for --lang=Java
    gnat gprbuild       # --lang=Ada: gprbuild builds the generated test.gpr
    gobjc               # gcc's Objective-C front end, for --lang=ObjC
    gnustep-devel       # the Foundation the generated ObjC assumes
    dotnet-sdk-10.0     # dotnet build, for --lang=CS
)

missing=()
for p in "${PKGS[@]}"; do
    dpkg -l "$p" 2>/dev/null | grep -q '^ii' || missing+=("$p")
done

if [ ${#missing[@]} -eq 0 ]; then
    echo "generator toolchains: already installed"
else
    echo "installing: ${missing[*]}"
    # A dpkg left half-configured by an earlier run makes apt refuse to do
    # anything at all, with no hint of which package is stuck. Found that way on
    # 2026-09-25, interrupted since the evening before.
    if ! sudo -n true 2>/dev/null; then
        echo "FATAL: needs sudo" >&2
        exit 1
    fi
    if dpkg -l 2>/dev/null | awk 'NR>5 && $1 !~ /^ii/ {bad=1} END {exit !bad}'; then
        echo "repairing an interrupted dpkg first"
        # wireshark-common asks whether non-root users may capture, and blocks
        echo 'wireshark-common wireshark-common/install-setuid boolean false' \
            | sudo debconf-set-selections
        sudo DEBIAN_FRONTEND=noninteractive dpkg --configure -a || exit 1
    fi
    export DEBIAN_FRONTEND=noninteractive
    sudo apt-get update -qq
    sudo apt-get install -y -qq "${missing[@]}" || exit 1
fi

echo "--- what is now available"
for c in javac gnatmake gprbuild dotnet gnustep-config swiftc flexspin; do
    printf '  %-16s %s\n' "$c" "$(command -v "$c" || echo 'MISSING')"
done
echo "  objc front end   $(gcc -x objective-c -E - </dev/null >/dev/null 2>&1 \
                           && echo ok || echo MISSING)"
