# -*- coding: utf-8 -*-
"""Historique de line profiling, versionne dans le depot, GRAIN PAR FONCTION.

Installe par Commun/scripts_installation/spyder_patch/patch_spyder_line_profiler_targets.py dans
site-packages/spyder_line_profiler/spyder/profile_history.py. Ce fichier est la source de
verite (versionne dans le depot) ; la copie dans site-packages est jetable.

Contexte (TODO CachyOS "TODO - Spyder - line profiler.txt", section "historique") :
    "peut-on enregistrer un fichier de line profiling par fichier python, avec la date, la
     machine, la revision du code ?"
    "si le code change (ajout, suppression, modification de quelques lignes), pourra-t-on
     retrouver quel resultat correspond a quelle ligne ?"

DECISIONS ARRETEES AVEC L'UTILISATEUR (22/07/2026)
  - Stockage NON dans ~/.config, NON en sidecar a cote de chaque fichier, mais dans un dossier
    ".profiler/" a la RACINE DU DEPOT (premier ancetre contenant .hg ou .git), un seul par depot,
    l'arborescence du projet recopiee dessous (cle = chemin RELATIF du fichier) -> l'historique
    voyage si on deplace tout le depot.
  - SUIVI PAR MERCURIAL (versionne) : chaque run est un fichier JSON NEUF et horodate
    (append-only), donc jamais de reecriture d'un fichier deja commite -> diffs propres. Ecrit
    trie et indente pour rester lisible en revision.
  - GRAIN PAR FONCTION, et non par fichier (correction du 22/07/2026). Les marqueurs sont poses
    fonction par fonction, INDEPENDAMMENT dans le temps : on peut profiler `calcul()` lundi, puis
    `dessine()` mardi. Un historique par fichier melerait ces runs et "le dernier profilage du
    fichier" masquerait une fonction jamais dé-mesuree. Par fonction, chaque fonction garde sa
    propre chronologie, et la vue courante d'un fichier = pour CHAQUE fonction, son run le plus
    recent - le cas "pas profilees en meme temps" se resout de lui-meme.
  - Un fichier hors depot, ou dans un depot en lecture seule (code de site-packages, SmartPython
    en lecture seule), n'a pas de racine inscriptible : pas d'historique pour lui (renvoi None,
    jamais d'exception). C'est le piege n°1 du depot -- une ecriture hors zone inscriptible
    "reussit" a vide -- qu'on evite en ne tentant meme pas l'ecriture.

ARBORESCENCE
    <racine du depot>/.profiler/<chemin relatif du fichier>/<nom qualifie>/<horodatage>_<machine>.json
    ex. .profiler/pkg/maths.py/Calc.methode/20260722T150312_cachyos.json

CONTENU D'UN RUN (format 2)
    date, machine, revision ; nom qualifie de la fonction et sa ligne de `def` au moment du
    profilage ; hash du contenu de la TRANCHE de la fonction (ancre robuste) ; la tranche de
    source elle-meme (def..fin, indispensable au remappage) ; l'unite de temps ; et les mesures,
    exprimees en lignes RELATIVES a la tranche ([offset_1based, hits, temps]).

POINT TECHNIQUE : line_profiler ne donne dans ses clefs que le NOM NU de la fonction (co_name,
ex. "methode") et sa ligne de `def`, pas le nom qualifie. Deux methodes "run" de deux classes se
confondraient. On RECONSTRUIT donc le nom qualifie ("Calc.methode") par ast, exactement comme les
marqueurs (profile_targets._function_at_line), pour clef d'historique.

Le module n'importe NI Qt NI Spyder (seulement ast, difflib, json, os, hashlib, platform,
subprocess) : il est testable avec n'importe quel interpreteur, sans serveur d'affichage.
"""

import ast
import difflib
import hashlib
import json
import os
import os.path as osp
import platform
import subprocess
from datetime import datetime


# Nom du dossier d'historique, a la racine du depot (choix de l'utilisateur, 22/07/2026).
HISTORY_DIRNAME = '.profiler'

# Marqueurs de racine de depot, dans l'ordre de preference.
VCS_MARKERS = ('.hg', '.git')

# Version du format JSON. 2 = grain par fonction, lignes relatives a la tranche (le format 1,
# par fichier, n'a jamais servi en reel : la refonte a precede toute utilisation).
FORMAT_VERSION = 2


# ---- Localisation du depot et du dossier d'historique
# -----------------------------------------------------------------------------
def find_repo_root(path):
    """Racine du depot de `path` : premier ancetre contenant .hg ou .git, ou None."""
    path = osp.abspath(path)
    directory = path if osp.isdir(path) else osp.dirname(path)
    while True:
        for marker in VCS_MARKERS:
            if osp.exists(osp.join(directory, marker)):
                return directory
        parent = osp.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def history_dir(path):
    """Dossier .profiler a la racine du depot de `path`, ou None hors depot."""
    root = find_repo_root(path)
    if root is None:
        return None
    return osp.join(root, HISTORY_DIRNAME)


def entry_dir(path):
    """Sous-dossier d'historique du FICHIER `path` (clef = chemin relatif au depot), ou None.

    Les runs d'une fonction vivent dans un sous-dossier de plus, nomme d'apres le nom qualifie
    de la fonction (cf. _function_dir).
    """
    root = find_repo_root(path)
    if root is None:
        return None
    rel = osp.relpath(osp.abspath(path), root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return osp.join(root, HISTORY_DIRNAME, rel)


def _sanitize(name):
    """Rend `name` sur pour un nom de dossier/fichier : garde lettres, chiffres, . _ - ; le reste
    devient '-'. Un nom qualifie ast ("Calc.methode") passe intact ; un nom de machine avec des
    caracteres exotiques ne fabrique pas de chemin dangereux."""
    return ''.join(c if (c.isalnum() or c in '._-') else '-' for c in name) or 'inconnu'


def _function_dir(path, qualname):
    """Dossier d'historique de la fonction `qualname` du fichier `path`, ou None hors depot."""
    entry = entry_dir(path)
    if entry is None:
        return None
    return osp.join(entry, _sanitize(qualname))


# ---- Analyse syntaxique : nom qualifie et etendue des fonctions
# -----------------------------------------------------------------------------
def _functions(tree):
    """{nom qualifie: (ligne_du_def, derniere_ligne)} pour les fonctions ciblables de `tree`.

    Meme politique que les marqueurs (profile_targets) : fonctions de premier niveau et methodes
    (y compris dans des classes imbriquees), MAIS pas les closures (une fonction dans une
    fonction n'est pas un attribut resoluble). `inside_function` fige la descente des qu'on entre
    dans une fonction.
    """
    result = {}

    def visit(node, prefix, inside_function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, prefix + [child.name], inside_function)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if inside_function:
                    continue
                qualname = '.'.join(prefix + [child.name])
                # child.lineno = ligne du `def` (les decorateurs sont dans decorator_list, avec
                # leurs propres numeros de ligne) ; end_lineno = derniere ligne du corps.
                result[qualname] = (child.lineno, child.end_lineno)
                visit(child, prefix + [child.name], True)
            else:
                visit(child, prefix, inside_function)

    visit(tree, [], False)
    return result


def qualname_at_line(source, lineno):
    """(nom qualifie, ligne_du_def, derniere_ligne) de la fonction contenant `lineno`.

    (None, None, None) si `lineno` n'est dans aucune fonction ou si le source est invalide. On
    retient la fonction la PLUS IMBRIQUEE qui contient la ligne (une methode l'emporte sur sa
    classe englobante).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return (None, None, None)
    best = None
    for qualname, (start, end) in _functions(tree).items():
        if start <= lineno <= end:
            if best is None or start > best[1]:   # plus grande ligne de def = plus imbriquee
                best = (qualname, start, end)
    if best is None:
        return (None, None, None)
    return best


def span_of_qualname(source, qualname):
    """(ligne_du_def, derniere_ligne) de la fonction `qualname` dans `source`, ou None."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    return _functions(tree).get(qualname)


# ---- Metadonnees d'un run
# -----------------------------------------------------------------------------
def content_hash(source):
    """SHA-256 du source, ancre robuste : identifie une version d'une tranche independamment de
    son emplacement ou de sa date, y compris si le fichier n'est pas commite."""
    return hashlib.sha256(source.encode('utf-8')).hexdigest()


def vcs_revision(path):
    """Identifiant de revision (hg puis git), ou None. N'echoue jamais bruyamment.

    hg d'abord (le depot SmartOS est en Mercurial), git en repli. Le "+" que hg ajoute signale un
    depot modifie : on le garde, c'est une information utile.
    """
    directory = osp.dirname(osp.abspath(path))
    for cmd in (['hg', 'id', '-i'], ['git', 'rev-parse', '--short', 'HEAD']):
        try:
            out = subprocess.run(cmd, cwd=directory, capture_output=True,
                                 text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


# ---- Ecriture et lecture des runs (par fonction)
# -----------------------------------------------------------------------------
def save_function_run(filename, qualname, def_line, source_slice, lines_rel, unit,
                      when=None, machine=None, revision=None):
    """Ecrit un run d'historique pour UNE fonction. Renvoie le chemin ecrit, ou None.

    Parametres :
      qualname     : nom qualifie ("Calc.methode"), sert de clef (sous-dossier).
      def_line     : ligne du `def` au moment du profilage (indicatif ; le remappage s'appuie sur
                     source_slice, pas sur ce numero).
      source_slice : tranche de source de la fonction (def..fin) au moment du profilage.
      lines_rel    : [(offset_1based_dans_la_tranche, hits, temps), ...].
      unit         : facteur de temps de LineStats (ex. 1e-09).
      when/machine/revision : injectables pour les tests ; sinon datetime.now(), platform.node()
                     et vcs_revision().

    Renvoie None SANS lever si le fichier n'est pas dans un depot inscriptible.
    """
    func_dir = _function_dir(filename, qualname)
    if func_dir is None:
        return None

    when = when or datetime.now()
    machine = machine or platform.node() or 'inconnu'
    if revision is None:
        revision = vcs_revision(filename)

    payload = {
        'format': FORMAT_VERSION,
        'datetime': when.isoformat(timespec='seconds'),
        'machine': machine,
        'revision': revision,
        'function': qualname,
        'def_line': def_line,
        'content_sha256': content_hash(source_slice),
        'source': source_slice,
        'unit': unit,
        'lines': [[int(rel), int(hits), temps] for (rel, hits, temps) in lines_rel],
    }

    stamp = when.strftime('%Y%m%dT%H%M%S')
    name = f'{stamp}_{_sanitize(machine)}.json'
    try:
        os.makedirs(func_dir, exist_ok=True)
        dest = osp.join(func_dir, name)
        with open(dest, 'w', encoding='utf-8') as stream:
            # sort_keys + indent : sortie stable et lisible en revision. Les listes gardent leur
            # ordre, sort_keys ne touche qu'aux dictionnaires.
            json.dump(payload, stream, ensure_ascii=False, indent=1, sort_keys=True)
    except OSError:
        return None
    return dest


def load_function_runs(filename, qualname):
    """Runs enregistres pour la fonction `qualname` de `filename`, du plus recent au plus ancien."""
    func_dir = _function_dir(filename, qualname)
    if func_dir is None or not osp.isdir(func_dir):
        return []
    runs = []
    for name in os.listdir(func_dir):
        if not name.endswith('.json'):
            continue
        try:
            with open(osp.join(func_dir, name), 'r', encoding='utf-8') as stream:
                runs.append(json.load(stream))
        except (OSError, ValueError):
            continue
    runs.sort(key=lambda run: run.get('datetime', ''), reverse=True)
    return runs


def functions_with_history(filename):
    """Noms qualifies des fonctions de `filename` ayant au moins un run enregistre.

    Lit les sous-dossiers de l'entree du fichier. Utile a la vue courante : pour chaque fonction
    connue, on ira chercher son run le plus recent.
    """
    entry = entry_dir(filename)
    if entry is None or not osp.isdir(entry):
        return []
    return sorted(name for name in os.listdir(entry)
                  if osp.isdir(osp.join(entry, name)))


def archive_lstats(lstats, when=None, machine=None, revision=None, save=save_function_run):
    """Archive un LineStats : UN run par fonction profilee. Renvoie la liste des runs ecrits.

    `lstats` est pris en canard : on ne lit que `.unit` et `.timings` (clefs
    (fichier, ligne_du_def, nom_nu) -> [(ligne, hits, temps), ...]). Aucun import de line_profiler
    ni de pickle ici - l'appelant (profile_results) deserialise et nous passe l'objet.

    Pour chaque fonction : on relit le source du fichier sur le disque, on reconstruit le nom
    qualifie et l'etendue de la fonction par ast (a partir de la ligne du `def` fournie par la
    clef), on decoupe la tranche def..fin, on exprime les mesures en lignes relatives a la
    tranche, et on ecrit le run. Les fichiers hors depot (save renvoie None) ou illisibles sont
    ignores.
    """
    unit = getattr(lstats, 'unit', 1e-6)
    sources = {}
    ecrits = []
    for (filename, def_line, funcname), timings in lstats.timings.items():
        if not timings:
            continue
        if filename not in sources:
            try:
                with open(filename, 'r', encoding='utf-8') as flux:
                    sources[filename] = flux.read()
            except OSError:
                sources[filename] = None
        source = sources[filename]
        if source is None:
            continue

        qualname, start, end = qualname_at_line(source, def_line)
        if qualname is None:
            # def_line ne tombe dans aucune fonction du source courant (fichier deja modifie
            # depuis le profilage, cas rare puisqu'on archive juste apres) : repli sur le nom nu,
            # tranche de la ligne du def a la derniere ligne mesuree.
            qualname = funcname
            start = def_line
            end = max(ligne for (ligne, _h, _t) in timings)

        lignes = source.splitlines()
        source_slice = '\n'.join(lignes[start - 1:end])
        lines_rel = [(ligne - start + 1, hits, temps) for (ligne, hits, temps) in timings]

        dest = save(filename, qualname, def_line, source_slice, lines_rel, unit,
                    when=when, machine=machine, revision=revision)
        if dest:
            ecrits.append(dest)
    return ecrits


# ---- Remappage d'un run vers le source courant (deuxieme question)
# -----------------------------------------------------------------------------
def remap_lines(old_source, new_source, line_values):
    """Reporte des valeurs indexees par ligne de `old_source` vers `new_source`.

    `line_values` : {ligne (base 1 dans old_source): valeur}. Renvoie (mapped, stale) :
      - mapped : {nouvelle_ligne: valeur} pour les lignes INCHANGEES entre les deux versions ;
      - stale  : {ancienne_ligne: valeur} pour les lignes modifiees ou supprimees.

    difflib aligne les deux suites de lignes ; seuls les blocs 'equal' donnent un report fiable.
    Une ligne editee n'a, par definition, plus de mesure valide -- l'ancien temps mesurait un
    autre code -- donc on la marque perimee plutot que de la reporter (mensonge silencieux).

    autojunk=False : sur du code, l'heuristique des "lignes populaires" de difflib ferait rater
    l'alignement des lignes courantes (accolades, "return", lignes vides).
    """
    old = old_source.splitlines()
    new = new_source.splitlines()
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)

    line_map = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for offset in range(i2 - i1):
                line_map[i1 + offset + 1] = j1 + offset + 1

    mapped, stale = {}, {}
    for lineno, value in line_values.items():
        new_lineno = line_map.get(lineno)
        if new_lineno is None:
            stale[lineno] = value
        else:
            mapped[new_lineno] = value
    return mapped, stale


def remap_run_to_current(run, current_source):
    """Reporte les mesures d'un run sur le source courant. Renvoie (mapped, stale).

    - mapped : {ligne ABSOLUE dans le fichier courant: entree [rel, hits, temps]} pour les lignes
      de la fonction restees identiques ;
    - stale  : {ligne relative dans la tranche du run: entree} pour les lignes modifiees,
      supprimees, ou toute la fonction si elle a disparu du fichier courant.

    On localise la fonction du run par son NOM QUALIFIE dans le source courant (robuste au
    decalage de tout le fichier : on n'aligne que le corps de la fonction), puis on aligne la
    tranche stockee avec la tranche courante. Une fonction disparue -> tout perime.
    """
    qualname = run.get('function')
    old_slice = run.get('source', '')
    entries = {int(rel): [int(rel), hits, temps] for (rel, hits, temps) in run.get('lines', [])}

    span = span_of_qualname(current_source, qualname) if qualname else None
    if span is None:
        return {}, entries

    cur_start, cur_end = span
    cur_slice = '\n'.join(current_source.splitlines()[cur_start - 1:cur_end])
    mapped_rel, stale_rel = remap_lines(old_slice, cur_slice, entries)
    # rel est 1-based DANS la tranche ; la tranche commence a cur_start -> absolu = cur_start-1+rel.
    mapped_abs = {cur_start - 1 + rel: value for rel, value in mapped_rel.items()}
    return mapped_abs, stale_rel
