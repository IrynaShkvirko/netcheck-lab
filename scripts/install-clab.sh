#!/usr/bin/env bash
# Install containerlab <version> from its GitHub release .deb, checked before dpkg sees it.
#
# The official installer (get.containerlab.dev) downloads without --fail, so an HTTP
# error page is saved as the .deb and dpkg reports "not a Debian format archive". Here
# every download fails on an HTTP error (with the status), is retried for a few minutes,
# and the package must match the release's checksums.txt and be a valid .deb.
set -euo pipefail

version="${1:?usage: install-clab.sh <version>}"
arch="$(dpkg --print-architecture)"
deb="containerlab_${version}_linux_${arch}.deb"
base="https://github.com/srl-labs/containerlab/releases/download/v${version}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fetch() {  # url dest: fail on HTTP errors, retry them (GitHub's downloads can 5xx briefly)
    echo "Downloading $1"
    curl --fail --silent --show-error --location \
        --retry 6 --retry-all-errors --retry-delay 20 --connect-timeout 20 \
        --output "$2" "$1"
}

fetch "$base/checksums.txt" "$tmp/checksums.txt"
fetch "$base/$deb" "$tmp/$deb"
echo "Downloaded $(stat -c %s "$tmp/$deb") bytes"

expected="$(awk -v f="$deb" '$2 == f {print $1}' "$tmp/checksums.txt")"
if [ -z "$expected" ]; then
    echo "error: $deb is not listed in checksums.txt of v${version}" >&2
    exit 1
fi
actual="$(sha256sum "$tmp/$deb" | awk '{print $1}')"
if [ "$actual" != "$expected" ]; then
    echo "error: sha256 of $deb is $actual, release says $expected" >&2
    echo "the download is not the release asset; it starts with:" >&2
    head -c 200 "$tmp/$deb" >&2 || true
    echo >&2
    exit 1
fi
dpkg-deb --info "$tmp/$deb" >/dev/null  # a valid .deb, or stop before installing

sudo dpkg -i "$tmp/$deb"
containerlab version
