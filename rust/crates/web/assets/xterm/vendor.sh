#!/bin/sh
# Vendor the pinned xterm.js packages (brief-m26 §1, design.md §3.6.9, PROTOCOL.md §15.4).
#
# Downloads exactly four tarballs from registry.npmjs.org, checks each against its npm sha512 integrity BEFORE
# extracting anything, copies the runtime files and each package's LICENSE next to this script, and writes
# MANIFEST.json (sha256 of every vendored file) and THIRD-PARTY-JS.txt. Run by hand only when the pins change
# (they never change without the supervisor); the results are committed. Builds and tests never run it.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/fleetmux-xterm.XXXXXX")
trap 'rm -rf "$tmp"' EXIT INT TERM

sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

# name version tarball integrity license-file runtime files (member:vendored-name …)
PKGS='@xterm/xterm|6.0.0|https://registry.npmjs.org/@xterm/xterm/-/xterm-6.0.0.tgz|sha512-TQwDdQGtwwDt+2cgKDLn0IRaSxYu1tSUjgKarSDkUM0ZNiSRXFpjxEsvc/Zgc5kq5omJ+V0a8/kIM2WD3sMOYg==|LICENSE-xterm|lib/xterm.js:xterm.js css/xterm.css:xterm.css
@xterm/addon-fit|0.11.0|https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.11.0.tgz|sha512-jYcgT6xtVYhnhgxh3QgYDnnNMYTcf8ElbxxFzX0IZo+vabQqSPAjC3c1wJrKB5E19VwQei89QCiZZP86DCPF7g==|LICENSE-addon-fit|lib/addon-fit.js:addon-fit.js
@xterm/addon-webgl|0.19.0|https://registry.npmjs.org/@xterm/addon-webgl/-/addon-webgl-0.19.0.tgz|sha512-b3fMOsyLVuCeNJWxolACEUED0vm7qC0cy4wRvf3oURSzDTYVQiGPhTnhWZwIHdvC48Y+oLhvYXnY4XDXPoJo6A==|LICENSE-addon-webgl|lib/addon-webgl.js:addon-webgl.js
@xterm/addon-unicode11|0.9.0|https://registry.npmjs.org/@xterm/addon-unicode11/-/addon-unicode11-0.9.0.tgz|sha512-FxDnYcyuXhNl+XSqGZL/t0U9eiNb/q3EWT5rYkQT/zuig8Gz/VagnQANKHdDWFM2lTMk9ly0EFQxxxtZUoRetw==|LICENSE-addon-unicode11|lib/addon-unicode11.js:addon-unicode11.js'

# 1. download + verify all four before touching anything here
echo "$PKGS" | while IFS='|' read -r name version url integrity lic files; do
  case "$url" in https://registry.npmjs.org/@xterm/*) ;; *) echo "refusing $url" >&2; exit 1 ;; esac
  f="$tmp/$(basename "$url")"
  curl -fsSL --proto '=https' --tlsv1.2 -o "$f" "$url"
  got="sha512-$(openssl dgst -sha512 -binary "$f" | openssl base64 -A)"
  if [ "$got" != "$integrity" ]; then echo "$name $version: integrity mismatch ($got)" >&2; exit 1; fi
  echo "$name $version: integrity ok"
done

# 2. extract, then write MANIFEST.json and THIRD-PARTY-JS.txt
manifest="$tmp/MANIFEST.json"; notices="$tmp/THIRD-PARTY-JS.txt"
printf '{"packages":[' > "$manifest"; : > "$notices"
first=1
echo "$PKGS" | {
  while IFS='|' read -r name version url integrity lic files; do
    f="$tmp/$(basename "$url")"
    tar -xzf "$f" -O package/LICENSE > "$here/$lic"
    entries="\"$lic\":\"$(sha256 "$here/$lic")\""
    for pair in $files; do
      member=${pair%%:*}; out=${pair#*:}
      tar -xzf "$f" -O "package/$member" > "$here/$out"
      entries="$entries,\"$out\":\"$(sha256 "$here/$out")\""
    done
    [ $first -eq 1 ] || printf ',' >> "$manifest"; first=0
    printf '\n{"name":"%s","version":"%s","tarball":"%s","integrity":"%s","files":{%s}}' \
      "$name" "$version" "$url" "$integrity" "$entries" >> "$manifest"
    { printf '%s %s\n%s\nLicense: MIT\n\n' "$name" "$version" "$url"; cat "$here/$lic"; printf '\n\n'; } >> "$notices"
  done
  printf '\n]}\n' >> "$manifest"
}
cp "$manifest" "$here/MANIFEST.json"; cp "$notices" "$here/THIRD-PARTY-JS.txt"
echo "vendored into $here"
