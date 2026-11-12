#!/bin/zsh

set -euo pipefail
setopt typesetsilent

EXPORT_DIR="${1:-$PWD/social_media_exports_2026-04-20}"
CAROUSEL_DIR="$EXPORT_DIR/carousels"

typeset -A TITLES=(
  [western-us]="Western US"
  [gorerange]="Gore Range"
  [1-high-parks-of-colorado]="High Parks of Colorado"
  [2-tomichi]="Tomichi"
  [3-minturn-gold]="Minturn Gold"
  [0-vail-mountain-winter-geography]="Vail Mountain Winter Geography"
  [4-vail-hachures]="Vail Hachures"
  [7-vail80smap]="Vail 80s Map"
  [6-denvertothegulf]="Denver to the Gulf"
  [grey-usgs-13-n40w107-20220216-cropped]="Grey USGS Crop"
  [blue-usgs-13-n40w107-20220216-cropped]="Blue USGS Crop"
  [red-usgs-13-n40w107-20220216-cropped]="Red USGS Crop"
)

typeset -A NOTES=(
  [regional-hero]="Broad-to-specific sequence for a strong general audience carousel."
  [vail-story]="Vail-focused set that stays geographically coherent."
  [color-study]="Three-color variant triptych with a neutral lead image."
)

typeset -A HOOKS=(
  [regional-hero]="A mountain story that starts regional and narrows into detail."
  [vail-story]="A compact visual story around Vail and the Gore Range."
  [color-study]="One terrain, three color moods."
)

typeset -A CTAS=(
  [regional-hero]="Which piece would you want to see on a wall?"
  [vail-story]="Which Vail-area piece should I post next on its own?"
  [color-study]="Which palette feels strongest to you: grey, blue, or red?"
)

typeset -A HASHTAGS=(
  [regional-hero]="#mountainart #mapart #topographicart #coloradomountains"
  [vail-story]="#vail #gorerange #skihistory #mountainart"
  [color-study]="#colorstudy #mapart #terrainart #printdesign"
)

typeset -a FORMATS=(
  "instagram_square|instagram_square_1080x1080"
  "instagram_portrait|instagram_portrait_1080x1350"
)

typeset -a REGIONAL_HERO=(
  "western-us"
  "gorerange"
  "1-high-parks-of-colorado"
  "2-tomichi"
  "3-minturn-gold"
  "0-vail-mountain-winter-geography"
  "4-vail-hachures"
  "7-vail80smap"
  "6-denvertothegulf"
)

typeset -a VAIL_STORY=(
  "0-vail-mountain-winter-geography"
  "4-vail-hachures"
  "gorerange"
  "3-minturn-gold"
  "7-vail80smap"
)

typeset -a COLOR_STUDY=(
  "grey-usgs-13-n40w107-20220216-cropped"
  "blue-usgs-13-n40w107-20220216-cropped"
  "red-usgs-13-n40w107-20220216-cropped"
)

mkdir -p "$CAROUSEL_DIR"

slugify_text() {
  print -r -- "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-+/-/g'
}

get_roles() {
  local carousel_name="$1"

  case "$carousel_name" in
    regional-hero)
      print -r -- "hook"
      print -r -- "region"
      print -r -- "overview"
      print -r -- "terrain"
      print -r -- "detail"
      print -r -- "winter"
      print -r -- "texture"
      print -r -- "archive"
      print -r -- "journey"
      ;;
    vail-story)
      print -r -- "hook"
      print -r -- "texture"
      print -r -- "range"
      print -r -- "detail"
      print -r -- "archive"
      ;;
    color-study)
      print -r -- "hook"
      print -r -- "cool"
      print -r -- "bold"
      ;;
    *)
      print -r -- "slide"
      ;;
  esac
}

write_caption_outline() {
  local carousel_name="$1"
  local format_name="$2"
  local out_dir="$3"
  shift 3
  local -a slides=("$@")
  local caption_file="$out_dir/CAPTION_OUTLINE.txt"
  local -a roles
  roles=("${(@f)$(get_roles "$carousel_name")}")
  local i=1

  {
    print -r -- "Carousel: ${carousel_name}"
    print -r -- "Format: ${format_name}"
    print -r -- ""
    print -r -- "Caption hook:"
    print -r -- "${HOOKS[$carousel_name]}"
    print -r -- ""
    print -r -- "Suggested caption outline:"
    case "$carousel_name" in
      regional-hero)
        print -r -- "1. Open with the wider geography and the idea of zooming from region to ridgeline."
        print -r -- "2. Mention that the sequence moves from broad mountain context into specific terrain studies."
        print -r -- "3. Call out one or two pieces that change the mood or scale of the set."
        print -r -- "4. Close with: ${CTAS[$carousel_name]}"
        print -r -- "5. Optional tags: #mountainart #mapart #topographicart #coloradomountains"
        ;;
      vail-story)
        print -r -- "1. Frame this as a tighter story around Vail, the Gore Range, and nearby terrain."
        print -r -- "2. Mention the shift from winter geography to texture, then to range-level context."
        print -r -- "3. Use the final historical piece as a nostalgic or archival payoff."
        print -r -- "4. Close with: ${CTAS[$carousel_name]}"
        print -r -- "5. Optional tags: #vail #gorerange #skihistory #mountainart"
        ;;
      color-study)
        print -r -- "1. Set up the post as a palette comparison on the same terrain."
        print -r -- "2. Explain that the first image anchors the form, then the cooler and warmer versions shift the mood."
        print -r -- "3. Ask viewers which version they would choose for print or wall display."
        print -r -- "4. Close with: ${CTAS[$carousel_name]}"
        print -r -- "5. Optional tags: #colorstudy #mapart #terrainart #printdesign"
        ;;
    esac
    print -r -- ""
    print -r -- "Slide-by-slide prompts:"
  } > "$caption_file"

  for slug in "${slides[@]}"; do
    local n role title
    n=$(printf "%02d" "$i")
    role="${roles[$i]:-slide}"
    title="${TITLES[$slug]}"
    print -r -- "${n}. ${role}: ${title}" >> "$caption_file"
    ((i++))
  done
}

write_full_caption() {
  local carousel_name="$1"
  local format_name="$2"
  local out_dir="$3"
  local caption_file="$out_dir/FULL_CAPTION.txt"

  {
    print -r -- "Carousel: ${carousel_name}"
    print -r -- "Format: ${format_name}"
    print -r -- ""
    case "$carousel_name" in
      regional-hero)
        cat <<EOF
This sequence starts with the broader mountain West and moves inward, from regional shape to specific ridgelines, valleys, and terrain studies that keep pulling the eye closer.

I like building these carousels this way because the first image gives the bigger context, and each slide after that becomes a deeper look at form, texture, and place. By the end, the story feels less like a single map and more like a progression through the landscape.

Which piece would you want to see on a wall?

${HASHTAGS[$carousel_name]}
EOF
        ;;
      vail-story)
        cat <<EOF
This one stays close to Vail and the Gore Range, moving from winter geography into texture, terrain, and a more archival view of the same mountain story.

I wanted this carousel to feel geographically tight but visually varied, so the sequence shifts from broad structure to finer surface detail and then lands on a piece with a little more history in it. Together they read like different ways of seeing the same place.

Which Vail-area piece should I post next on its own?

${HASHTAGS[$carousel_name]}
EOF
        ;;
      color-study)
        cat <<EOF
Same terrain, three different moods.

I like seeing how a palette change can completely shift the feeling of a piece without changing the underlying landform. Starting with the neutral version makes the structure clear, and the cooler and warmer versions each push the terrain in a different emotional direction.

Which palette feels strongest to you: grey, blue, or red?

${HASHTAGS[$carousel_name]}
EOF
        ;;
    esac
  } > "$caption_file"
}

write_carousel() {
  local carousel_name="$1"
  local format_name="$2"
  local format_suffix="$3"
  shift 3
  local -a slides=("$@")
  local out_dir="$CAROUSEL_DIR/${carousel_name}_${format_name}"
  local guide="$out_dir/ORDER.txt"
  local caption_dir="$out_dir/caption_ready"
  local -a roles
  local i=1

  roles=("${(@f)$(get_roles "$carousel_name")}")

  rm -rf "$out_dir"
  mkdir -p "$out_dir"
  mkdir -p "$caption_dir"

  {
    print -r -- "Carousel: ${carousel_name}"
    print -r -- "Format: ${format_name}"
    print -r -- "Note: ${NOTES[$carousel_name]}"
    print -r -- ""
    print -r -- "Recommended slide order:"
  } > "$guide"

  for slug in "${slides[@]}"; do
    local src="$EXPORT_DIR/$slug/${slug}_${format_suffix}.jpg"
    local n
    n=$(printf "%02d" "$i")
    local safe_title
    local role
    role="${roles[$i]:-slide}"
    safe_title="$(slugify_text "${TITLES[$slug]}")"
    local dst="$out_dir/${n}_${safe_title}.jpg"
    local caption_dst="$caption_dir/slide-${n}-${role}-${safe_title}.jpg"

    if [[ ! -f "$src" ]]; then
      print -u2 "Missing slide source: $src"
      exit 1
    fi

    cp "$src" "$dst"
    cp "$src" "$caption_dst"
    print -r -- "${n},${TITLES[$slug]},$(basename "$dst")" >> "$guide"
    ((i++))
  done

  write_caption_outline "$carousel_name" "$format_name" "$out_dir" "${slides[@]}"
  write_full_caption "$carousel_name" "$format_name" "$out_dir"
}

for format_spec in "${FORMATS[@]}"; do
  IFS='|' read -r format_name format_suffix <<< "$format_spec"
  write_carousel "regional-hero" "$format_name" "$format_suffix" "${REGIONAL_HERO[@]}"
  write_carousel "vail-story" "$format_name" "$format_suffix" "${VAIL_STORY[@]}"
  write_carousel "color-study" "$format_name" "$format_suffix" "${COLOR_STUDY[@]}"
done

cat > "$CAROUSEL_DIR/README.txt" <<'EOF'
Carousel folders generated from the exported social media assets.

Included sets:
- regional-hero: broad-to-specific sequence for a general audience post
- vail-story: Vail-focused sequence
- color-study: grey/blue/red variant sequence

Formats:
- instagram_square
- instagram_portrait

Each folder contains:
- numbered slide JPGs in posting order
- ORDER.txt with the recommended sequence
- caption_ready/ with publish-friendly filenames
- CAPTION_OUTLINE.txt with a matching caption structure
- FULL_CAPTION.txt with a complete draft caption
EOF

print -r -- "Carousel folders written to: $CAROUSEL_DIR"
