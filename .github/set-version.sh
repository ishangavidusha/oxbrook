#!/usr/bin/env bash
# Set the crate's version, on any runner this pipeline uses.
#
# `sed` rather than a Python script, because every runner has bash and they do
# not all agree on what a Python is called. Two things about that sed are not
# optional:
#
# * `-i.bak`: BSD sed, which is macOS's, reads the next argument as the backup
#   suffix without one and fails on the file name.
# * no `0,/re/` line address: that is a GNU extension, and BSD sed quietly
#   changed nothing at all, so macOS built wheels carrying the version that was
#   already in the file. `^version = ` appears once, in `[package]`, because
#   every dependency is an inline table whose line starts with its own name.
#
# And the substitution is checked, because the failure that got through was a
# command that succeeded without doing anything.
set -euo pipefail

version="$1"
sed -i.bak "s/^version = \".*\"$/version = \"$version\"/" Cargo.toml
rm -f Cargo.toml.bak
grep -q "^version = \"$version\"$" Cargo.toml || {
  echo "::error::Cargo.toml still says $(sed -n 's/^version = "\(.*\)"$/\1/p' Cargo.toml), not $version"
  exit 1
}
echo "Cargo.toml version set to $version"
