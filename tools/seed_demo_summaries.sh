#!/usr/bin/env bash
# seed_demo_summaries.sh — Drop a few hand-written summaries into the
# PRD-018 store so the knowledge panel demos against a couple of well-
# known queries. Temporary: replaced by the real offline pipeline once
# M3-M6 ship.
#
# Run on prod:
#   sudo bash /opt/zettair-search/tools/seed_demo_summaries.sh
#
# Restarts the service at the end so the new offsets are loaded.

set -euo pipefail

STORE=/mnt/wikipedia-source/summaries.store
MAP=/mnt/wikipedia-source/summaries.map
TOOL=/opt/zettair-search/tools/summaries_admin.py

add() {
    local query="$1"; shift
    local body="$1"; shift
    sudo -u zettair python3 "$TOOL" --store "$STORE" --map "$MAP" add "$query" "$body"
}

add "morrissey" "**Morrissey** is an English singer and songwriter, born Steven Patrick Morrissey in 1959. He fronted The Smiths from 1982 to 1987, then launched a long solo career.

- Frontman of The Smiths (1982-1987)
- 12+ solo studio albums since *Viva Hate* (1988)
- Outspoken vegan and animal-rights advocate
- Born in Davyhulme, Lancashire"

add "albert einstein" "**Albert Einstein** (1879-1955) was a German-born theoretical physicist who developed the theory of relativity, one of the two pillars of modern physics alongside quantum mechanics.

- 1921 Nobel Prize in Physics for the photoelectric effect
- Famous mass-energy equivalence: E=mc²
- Emigrated to the US in 1933
- Worked at the Institute for Advanced Study, Princeton"

add "photosynthesis" "**Photosynthesis** is the process plants, algae, and some bacteria use to convert light energy into chemical energy stored in carbohydrates. It releases oxygen as a byproduct.

- Takes place in chloroplasts using the pigment chlorophyll
- Two stages: light-dependent reactions and the Calvin cycle
- Inputs: water, carbon dioxide, light
- Outputs: glucose and oxygen"

add "london" "**London** is the capital and largest city of England and the United Kingdom, on the River Thames. Founded by the Romans as Londinium around AD 47, it is one of the worlds oldest continuously inhabited cities.

- Population: ~9 million (Greater London)
- Home to the Houses of Parliament, Buckingham Palace, and Big Ben
- Global financial centre; the City of London is the historic core
- Hosted the Summer Olympics in 1908, 1948, and 2012"

add "denver" "**Denver** is the capital and most populous city of Colorado, sitting one mile above sea level on the High Plains east of the Rocky Mountains. Founded in 1858 during the Pike's Peak Gold Rush.

- Population: ~715,000 (city), ~3 million (metro)
- Nickname: the Mile-High City
- Home to the Broncos, Nuggets, Avalanche, and Rockies
- Denver International Airport is the largest US airport by area"

sudo systemctl restart zettair-search
echo "Done. Try: https://zettair.io/?q=morrissey"
