#!/bin/zsh

set -euo pipefail

SOURCE_DIR="/Users/michaelfloyd/Library/CloudStorage/Dropbox/0-Drafts to print"
STAMP="$(date +%Y-%m-%d)"
OUTPUT_DIR="${1:-$PWD/social_media_exports_${STAMP}}"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/social_exports.XXXXXX")"

typeset -a SOURCES=(
  "0-Vail Mountain Winter Geography.pdf"
  "1-High Parks of Colorado.pdf"
  "2-Tomichi.pdf"
  "3-Minturn_Gold.pdf"
  "4-Vail_hachures.pdf"
  "6-DenvertotheGulf.pdf"
  "7-Vail80sMap.pdf"
  "GoreRange.pdf"
  "Western_US.png"
  "Red – USGS_13_n40w107_20220216 cropped.png"
  "Blue – USGS_13_n40w107_20220216 cropped.png"
  "Grey – USGS_13_n40w107_20220216 cropped.png"
)

typeset -a EXPORTS=(
  "instagram_portrait|1350|1080|crop|Instagram portrait post"
  "instagram_square|1080|1080|crop|Instagram square post"
  "story|1920|1080|pad|Story/Reel cover with full-art padding"
  "x_landscape|900|1600|crop|X or LinkedIn landscape post"
)

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$OUTPUT_DIR"

MANIFEST="$OUTPUT_DIR/manifest.csv"
README="$OUTPUT_DIR/README.txt"

cat > "$MANIFEST" <<'EOF'
source,slug,format,width,height,mode,output_path
EOF

cat > "$README" <<EOF
Social media exports generated on ${STAMP}

Formats:
- instagram_portrait: 1080x1350 cropped for feed posts
- instagram_square: 1080x1080 cropped for grid posts
- story: 1080x1920 padded to keep the full artwork visible
- x_landscape: 1600x900 cropped for X or LinkedIn style posts

Organization:
- Each artwork has its own folder
- Files are grouped by platform-ready format
- manifest.csv lists every generated asset
EOF

slugify() {
  local input="$1"
  local ascii
  ascii="$(
    {
      print -r -- "$input" \
        | tr '[:upper:]' '[:lower:]' \
        | iconv -c -f UTF-8 -t ASCII//TRANSLIT 2>/dev/null
    } || true
  )"

  if [[ -z "$ascii" ]]; then
    ascii="$(print -r -- "$input" | tr '[:upper:]' '[:lower:]')"
  fi

  print -r -- "$ascii" \
    | sed -E 's/\.[^.]+$//' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-+/-/g'
}

get_dims() {
  local file="$1"
  sips -g pixelWidth -g pixelHeight "$file" \
    | awk '/pixelWidth:/ {w=$2} /pixelHeight:/ {h=$2} END {print w " " h}'
}

render_source() {
  local src="$1"
  local rendered="$2"
  case "${src:e:l}" in
    pdf)
      sips -s format png "$src" --out "$rendered" >/dev/null
      ;;
    *)
      cp "$src" "$rendered"
      ;;
  esac
}

make_crop_export() {
  local src="$1"
  local out="$2"
  local target_h="$3"
  local target_w="$4"
  local dims src_w src_h crop_w crop_h cropped

  dims=($(get_dims "$src"))
  src_w="${dims[1]}"
  src_h="${dims[2]}"

  crop_w="$(awk -v sw="$src_w" -v sh="$src_h" -v tw="$target_w" -v th="$target_h" 'BEGIN {
    if ((sw / sh) > (tw / th)) {
      printf "%d", sh * tw / th
    } else {
      printf "%d", sw
    }
  }')"

  crop_h="$(awk -v sw="$src_w" -v sh="$src_h" -v tw="$target_w" -v th="$target_h" 'BEGIN {
    if ((sw / sh) > (tw / th)) {
      printf "%d", sh
    } else {
      printf "%d", sw * th / tw
    }
  }')"

  cropped="$TMP_DIR/$(basename "$out").cropped.png"
  sips -c "$crop_h" "$crop_w" "$src" --out "$cropped" >/dev/null
  sips -z "$target_h" "$target_w" "$cropped" >/dev/null
  sips -s format jpeg -s formatOptions best "$cropped" --out "$out" >/dev/null
}

make_pad_export() {
  local src="$1"
  local out="$2"
  local target_h="$3"
  local target_w="$4"
  local dims src_w src_h fit_w fit_h resized padded

  dims=($(get_dims "$src"))
  src_w="${dims[1]}"
  src_h="${dims[2]}"

  fit_w="$(awk -v sw="$src_w" -v sh="$src_h" -v tw="$target_w" -v th="$target_h" 'BEGIN {
    if ((sw / sh) > (tw / th)) {
      printf "%d", tw
    } else {
      if (sh == 0) {
        printf "%d", tw
      } else {
        printf "%d", sw * th / sh
      }
    }
  }')"

  fit_h="$(awk -v sw="$src_w" -v sh="$src_h" -v tw="$target_w" -v th="$target_h" 'BEGIN {
    if ((sw / sh) > (tw / th)) {
      if (sw == 0) {
        printf "%d", th
      } else {
        printf "%d", sh * tw / sw
      }
    } else {
      printf "%d", th
    }
  }')"

  resized="$TMP_DIR/$(basename "$out").resized.png"
  padded="$TMP_DIR/$(basename "$out").padded.png"
  cp "$src" "$resized"
  sips -z "$fit_h" "$fit_w" "$resized" >/dev/null
  sips -p "$target_h" "$target_w" --padColor F7F4EE "$resized" --out "$padded" >/dev/null
  sips -s format jpeg -s formatOptions best "$padded" --out "$out" >/dev/null
}

for source_name in "${SOURCES[@]}"; do
  source_path="$SOURCE_DIR/$source_name"
  if [[ ! -f "$source_path" ]]; then
    print -u2 "Skipping missing source: $source_name"
    continue
  fi

  base_slug="$(slugify "$source_name")"
  art_dir="$OUTPUT_DIR/$base_slug"
  mkdir -p "$art_dir"

  rendered="$TMP_DIR/${base_slug}.png"
  render_source "$source_path" "$rendered"

  for export_spec in "${EXPORTS[@]}"; do
    IFS='|' read -r export_name target_h target_w mode label <<< "$export_spec"
    out_path="$art_dir/${base_slug}_${export_name}_${target_w}x${target_h}.jpg"

    if [[ "$mode" == "crop" ]]; then
      make_crop_export "$rendered" "$out_path" "$target_h" "$target_w"
    else
      make_pad_export "$rendered" "$out_path" "$target_h" "$target_w"
    fi

    print -r -- "\"$source_name\",$base_slug,$export_name,$target_w,$target_h,$mode,\"$out_path\"" >> "$MANIFEST"
  done
done

print -r -- "Exports written to: $OUTPUT_DIR"
