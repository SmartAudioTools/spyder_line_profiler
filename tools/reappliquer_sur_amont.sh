#!/bin/bash
# Reconstruit la branche smartos de ce fork sur une nouvelle version amont de
# spyder-line-profiler, sans rebasage : on repart de l'etiquette officielle et on rejoue
# les deux correctifs AST (idempotents, echec bruyant si l'amont a change de forme), qui
# copient aussi les modules runtime (lp_launcher, profile_*) depuis ../spyder_line_profiler
# ... c'est-a-dire depuis CE depot : les modules vivent ICI, dans
# spyder_line_profiler/spyder/, et sont recopies tels quels.
#
# Usage : tools/reappliquer_sur_amont.sh <version amont, ex. 0.4.3>
set -eu
VERSION="${1:?Usage: reappliquer_sur_amont.sh <version amont>}"
ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ICI"
# Sauvegarde des modules runtime et des outils AVANT le reset (ils vivent sur la branche
# qu'on va reconstruire).
SAUVE="$(mktemp -d)"
trap 'rm -rf "$SAUVE"' EXIT
cp spyder_line_profiler/spyder/{lp_launcher,profile_cprofile,profile_history,profile_results,profile_targets}.py "$SAUVE/"
cp -r tools smartos_tests "$SAUVE/"
git fetch upstream "refs/tags/v${VERSION}:refs/tags/v${VERSION}" --force
git checkout -B smartos "refs/tags/v${VERSION}"
git clean -fdx && git reset --hard "refs/tags/v${VERSION}"
cp -r "$SAUVE/tools" "$SAUVE/smartos_tests" .
cp "$SAUVE"/*.py spyder_line_profiler/spyder/
python3 tools/patch_spyder_line_profiler_targets.py spyder_line_profiler/spyder
python3 tools/patch_spyder_lineprofiler_toolbar.py spyder_line_profiler/spyder/widgets.py
git add -A
git commit -m "spyder-line-profiler ${VERSION} + modules et correctifs SmartOS"
echo "Termine : branche smartos reconstruite sur v${VERSION}. Pousser avec : git push -f origin smartos"
