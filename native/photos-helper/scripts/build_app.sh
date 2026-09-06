#!/bin/sh
set -eu

package_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_root="$package_root/build"
app_root="$build_root/PhotoArchiveMediaHelper.app"
module_root="$build_root/module"
target_arch=$(uname -m)

mkdir -p "$app_root/Contents/MacOS" "$module_root"
cp "$package_root/Info.plist" "$app_root/Contents/Info.plist"

swiftc \
  -parse-as-library \
  -emit-module \
  -emit-library \
  -static \
  -module-name PhotosHelperCore \
  -target "$target_arch-apple-macosx14.0" \
  -emit-module-path "$module_root/PhotosHelperCore.swiftmodule" \
  "$package_root/Sources/PhotosHelperCore/Protocol.swift" \
  "$package_root/Sources/PhotosHelperCore/MediaMetadata.swift" \
  "$package_root/Sources/PhotosHelperCore/PhotoLibraryAdapter.swift" \
  "$package_root/Sources/PhotosHelperCore/PhoneDeviceAdapter.swift" \
  -o "$module_root/libPhotosHelperCore.a"

swiftc \
  -target "$target_arch-apple-macosx14.0" \
  -I "$module_root" \
  -L "$module_root" \
  "$package_root/Sources/photos-helper/main.swift" \
  -lPhotosHelperCore \
  -framework AppKit \
  -framework AVFoundation \
  -framework CryptoKit \
  -framework ImageCaptureCore \
  -framework ImageIO \
  -framework Photos \
  -framework UniformTypeIdentifiers \
  -o "$app_root/Contents/MacOS/photos-helper"

codesign --force --deep --options runtime \
  --entitlements "$package_root/PhotoArchiveMediaHelper.entitlements" \
  --requirements '=designated => identifier "com.photoarchive.photos-helper"' \
  --sign - "$app_root"

lsregister="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$lsregister" ]; then
  "$lsregister" -f "$app_root" >/dev/null 2>&1 || true
fi

"$app_root/Contents/MacOS/photos-helper" self-test --request-id build-self-test \
  | grep '"contract":"ok"' >/dev/null

echo "$app_root"
