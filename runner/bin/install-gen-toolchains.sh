#!/bin/bash
# Compilers for the languages pymavlink generates, so a review of a generator
# change can run the generated code instead of reading it.
#
# pymavlink's own CI generates but never compiles, and neither did this box:
# eight generator paths were read-verified only. These are the ones apt can
# supply; Swift and flexspin are not packaged for Ubuntu and are installed
# below, from swift.org with its signature checked and from flexspin's own
# source respectively.
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

# --- the two that apt cannot supply -------------------------------------------
# Both are third-party binaries on a box that holds live credentials, so Swift
# is signature-checked against the key swift.org publishes, and flexspin is
# built here from its own source rather than fetched as a binary.

SWIFT_VER=6.3.3
# No Ubuntu 26.04 build exists; the 24.04 one runs on it unchanged.
SWIFT_TAR=swift-$SWIFT_VER-RELEASE-ubuntu24.04.tar.gz
SWIFT_URL=https://download.swift.org/swift-$SWIFT_VER-release/ubuntu2404/swift-$SWIFT_VER-RELEASE/$SWIFT_TAR
SWIFT_DIR=/opt/swift-$SWIFT_VER

install_swift() {
    if [ -x "$SWIFT_DIR/usr/bin/swiftc" ]; then
        echo "swift: already at $SWIFT_DIR"
        return 0
    fi
    local d
    d=$(mktemp -d "${TMPDIR:-/var/tmp}/swift-XXXXXX") || return 1
    echo "swift: fetching $SWIFT_VER (about 1GB)"
    curl -fsSL --max-time 1800 -o "$d/$SWIFT_TAR" "$SWIFT_URL" || { rm -rf "$d"; return 1; }
    curl -fsSL --max-time 300 -o "$d/sig" "$SWIFT_URL.sig" || { rm -rf "$d"; return 1; }
    # the published keyring is gzipped despite the .asc name
    curl -fsSL --max-time 300 https://www.swift.org/keys/all-keys.asc \
        | gunzip -c | gpg --quiet --import || { rm -rf "$d"; return 1; }
    # The 6.x release key expires and is not renewed, so trust is in the
    # signature being good, not in the key still being current.
    if ! gpg --verify "$d/sig" "$d/$SWIFT_TAR" 2>&1 | grep -q '^gpg: Good signature'; then
        echo "FATAL: $SWIFT_TAR did not verify against the swift.org key" >&2
        rm -rf "$d"
        return 1
    fi
    echo "swift: good signature, installing"
    sudo mkdir -p "$SWIFT_DIR" \
        && sudo tar xzf "$d/$SWIFT_TAR" -C "$SWIFT_DIR" --strip-components=1 || {
            rm -rf "$d"; return 1; }
    rm -rf "$d"
    local b
    for b in swiftc swift swift-frontend; do
        sudo ln -sfn "$SWIFT_DIR/usr/bin/$b" "/usr/local/bin/$b"
    done
}

FLEX_DIR=/opt/flexspin

install_flexspin() {
    if [ -x "$FLEX_DIR/flexspin" ]; then
        echo "flexspin: already at $FLEX_DIR"
        return 0
    fi
    local d
    d=$(mktemp -d "${TMPDIR:-/var/tmp}/flexspin-XXXXXX") || return 1
    echo "flexspin: building from source"
    git clone -q --depth 1 https://github.com/totalspectrum/spin2cpp.git "$d/src" || {
        rm -rf "$d"; return 1; }
    make -C "$d/src" -j"$(nproc)" >/dev/null 2>&1 || { rm -rf "$d"; return 1; }
    # Lib/ as well: flexspin needs it on the include path to resolve anything
    sudo mkdir -p "$FLEX_DIR" \
        && sudo cp -a "$d/src/build/flexspin" "$d/src/build/flexcc" \
                      "$d/src/build/spin2cpp" "$d/src/Lib" "$FLEX_DIR/" || {
            rm -rf "$d"; return 1; }
    rm -rf "$d"
    local b
    for b in flexspin flexcc spin2cpp; do
        sudo ln -sfn "$FLEX_DIR/$b" "/usr/local/bin/$b"
    done
}

install_swift    || echo "WARNING: swift not installed; --lang=Swift stays read-only" >&2
install_flexspin || echo "WARNING: flexspin not installed; --lang=Spin2 stays read-only" >&2

echo "--- what is now available"
for c in javac gnatmake gprbuild dotnet gnustep-config swiftc flexspin; do
    printf '  %-16s %s\n' "$c" "$(command -v "$c" || echo 'MISSING')"
done
echo "  objc front end   $(gcc -x objective-c -E - </dev/null >/dev/null 2>&1 \
                           && echo ok || echo MISSING)"
