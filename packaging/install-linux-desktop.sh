#!/bin/sh
# Register this portable copy for the current user; no Python or root needed.
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$app_dir/SAR" ] || [ ! -f "$app_dir/SAR.svg" ]; then
    printf '%s\n' '请在完整解压的 SAR 目录中运行此脚本。' >&2
    exit 1
fi
case "${XDG_DATA_HOME:-}" in
    /*) data_dir=$XDG_DATA_HOME ;;
    *) data_dir=${HOME:?HOME is not set}/.local/share ;;
esac
control_free=$(printf '%s' "$app_dir$data_dir" | LC_ALL=C tr -d '\000-\037\177')
if [ "$control_free" != "$app_dir$data_dir" ]; then
    printf '%s\n' '应用路径不能包含控制字符。' >&2
    exit 1
fi
helper_dir=$data_dir/sar-downloader
applications=$data_dir/applications
icons=$data_dir/icons/hicolor/scalable/apps
mkdir -p "$helper_dir" "$applications" "$icons"
umask 077
helper_tmp=$(mktemp "$helper_dir/.launch.XXXXXX")
desktop_tmp=$(mktemp "$applications/.sar-downloader.XXXXXX")
icon_tmp=$(mktemp "$icons/.sar-downloader.XXXXXX")
shortcut_tmp=
trap 'rm -f "$helper_tmp" "$desktop_tmp" "$icon_tmp"; if [ -n "$shortcut_tmp" ]; then rm -f "$shortcut_tmp"; fi' 0
trap 'exit 1' HUP INT TERM

# Single-quote the program path for a POSIX shell, including literal apostrophes.
shell_path=$(printf '%s' "$app_dir/SAR" | sed "s/'/'\\\\''/g")
printf '#!/bin/sh\nexec '\''%s'\'' "$@"\n' "$shell_path" > "$helper_tmp"
chmod 700 "$helper_tmp"
cp "$app_dir/SAR.svg" "$icon_tmp"
chmod 644 "$icon_tmp"

# Desktop Entry strings have one escape layer before Exec argument parsing.
exec_path=$(printf '%s' "$helper_dir/launch.sh" | awk '
    BEGIN { ORS="" }
    {
        for (i=1; i<=length($0); i++) {
            c=substr($0,i,1)
            if (c=="\\") printf "\\\\\\\\"
            else if (c=="\"" || c=="$" || c==sprintf("%c",96)) printf "\\\\%s",c
            else if (c=="%") printf "%%%%"
            else printf "%s",c
        }
    }')
icon_path=$(printf '%s' "$icons/sar-downloader.svg" | sed 's/\\/\\\\/g')
{
    printf '%s\n' '[Desktop Entry]' 'Type=Application' 'Name=SAR Downloader'
    printf '%s\n' 'Comment=SAR imagery and elevation data downloader'
    printf 'Exec=/bin/sh "%s"\n' "$exec_path"
    printf 'Icon=%s\n' "$icon_path"
    printf '%s\n' 'Terminal=false' 'StartupNotify=false' 'Categories=Science;Geoscience;'
} > "$desktop_tmp"
chmod 644 "$desktop_tmp"
mv -f "$helper_tmp" "$helper_dir/launch.sh"
mv -f "$icon_tmp" "$icons/sar-downloader.svg"
mv -f "$desktop_tmp" "$applications/sar-downloader.desktop"

# Also provide an icon launcher beside the portable program when writable.
if [ -w "$app_dir" ]; then
    shortcut_tmp=$(mktemp "$app_dir/.sar-shortcut.XXXXXX")
    cp "$applications/sar-downloader.desktop" "$shortcut_tmp"
    chmod 755 "$shortcut_tmp"
    mv -f "$shortcut_tmp" "$app_dir/SAR Downloader.desktop"
    shortcut_tmp=
    if command -v gio >/dev/null 2>&1; then
        gio set "$app_dir/SAR Downloader.desktop" metadata::trusted true >/dev/null 2>&1 || :
    fi
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$applications" >/dev/null 2>&1 || :
fi
printf '%s\n' '已设置 SAR Downloader 应用图标，可从应用菜单或同目录的图标入口启动。'
printf '%s\n' '移动程序目录后，请重新运行本脚本。'
