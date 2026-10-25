#!/bin/bash
# Builds letterbox_bridge.dll with MinGW-w64 (Arch: sudo pacman -S mingw-w64-gcc). Everything is linked
# statically, so the DLL needs nothing but Windows (or Wine/Proton).
set -e
cd "$(dirname "$0")"
x86_64-w64-mingw32-g++ -std=c++17 -O2 -shared -static -static-libgcc -static-libstdc++ -s \
    -Wall -Wno-unknown-pragmas -o letterbox_bridge.dll bridge.cpp
echo "built $(pwd)/letterbox_bridge.dll"
