# -*- coding: utf-8 -*-
"""Coloration de TOUT le code depuis le profilage normal (cProfile) : coeur pur, testable seul.

Installe par Commun/scripts_installation/spyder_patch/patch_spyder_line_profiler_targets.py dans
site-packages/spyder_line_profiler/spyder/profile_cprofile.py. Source de verite versionnee ; la
copie de site-packages est jetable.

Contexte et CONCEPTION COMPLETE : TODO - Spyder - line profiler.txt, section "colorer TOUT le
code depuis le profilage normal (cProfile)". Modele ARRETE (echange 23/07/2026) :

  - On colore la LIGNE DU `def` de chaque fonction avec son cumtime (temps cumule, sous-appels
    compris). Le corps n'est PAS colore a plat (ce serait inventer une distribution par ligne
    qu'on n'a pas) : il ne se colore que si la fonction est line-profilee (couche line profiler,
    par-dessus, inchangee).

  - METRIQUE = CUMTIME BRUT, une SEULE echelle pour le `def` et le corps, normalisee sur
    `total_tt` (le temps total du programme, donne directement par cProfile) :
        heat = log_heat(valeur, total_tt, plancher)     plancher ≈ 0.1 % de total_tt
    valeur = cumtime pour un `def`, temps par ligne reel pour une ligne de corps marquee.

  - POURQUOI cumtime brut et non cumtime/nb_lignes : CONTRAINTE DE COHERENCE de l'utilisateur -
    si A appelle B et que A est line-profilee, la ligne "appeler B" dans A doit avoir la MEME
    couleur que la ligne du `def` de B. Or le line profiler donne a cette ligne d'appel la valeur
    = cumtime de B (le temps par ligne inclut TOUJOURS le sous-appel, meme marque - mesure du
    23/07/2026). Pour que les deux matchent, le `def` de B doit donc porter le cumtime BRUT de B.
    Consequence assumee : main (plus gros cumtime) = total_tt = le plus chaud ; la chaleur remonte
    tout le chemin d'appel (cumule) - c'est voulu, on suit la couleur vers le bas jusqu'a l'endroit
    ou le cumtime chute, le vrai puits.

CE MODULE (pur : pstats, math ; NI Qt NI Spyder, NI ast, NI lecture de source) fournit :
  - read_prof : lit un .prof en {(fichier, ligne_def, fonction): (cumtime, tottime, ncalls)} + le
    temps total (pstats.total_tt), qui est le haut de l'echelle.
  - log_heat : la formule log/plancher (LE point a tester, defaut invisible).
  - def_values_for_file : les valeurs a poser sur les lignes de `def` d'un fichier (cumtime par
    fonction) - la ligne du `def` vient directement de cProfile, aucun source ni ast necessaire.

C'est le consommateur (profile_results.py) qui applique log_heat (avec total_tt et le plancher)
et peint, en laissant la couche line profiler colorer le corps des fonctions marquees.
"""

import math
import os.path as osp
import pstats


# ---- Lecture du .prof cProfile
# -----------------------------------------------------------------------------
def read_prof(path):
    """Lit un fichier .prof (pstats). Renvoie (stats, total_tt).

    stats : {(fichier, ligne_du_def, nom_fonction): (cumtime, tottime, ncalls)}.
        cumtime = temps cumule (sous-appels compris), c'est lui qu'on colore ; tottime = temps
        propre (pour l'infobulle) ; ncalls = nombre d'appels. Les entrees sans fichier source
        (fonctions C, "<built-in ...>") sont gardees telles quelles - def_values_for_file les
        ignore faute de fichier a colorer.
    total_tt : temps total du programme = haut de l'echelle (le plus gros cumtime possible).

    Renvoie ({}, 0.0) si le fichier est absent ou illisible : un profilage manquant ne doit
    jamais empecher l'ouverture d'un editeur.
    """
    try:
        stats_obj = pstats.Stats(path)
    except Exception:  # pstats est avare en types d'exceptions documentes
        return {}, 0.0

    result = {}
    for key, value in stats_obj.stats.items():
        # key = (fichier, ligne, nom) ; value = (cc, nc, tt, ct, callers)
        # cc = appels primitifs, nc = appels totaux, tt = tottime, ct = cumtime.
        _cc, nc, tt, ct, _callers = value
        result[key] = (ct, tt, nc)
    return result, getattr(stats_obj, 'total_tt', 0.0)


# ---- Formule de chaleur : logarithmique avec plancher (LE point delicat)
# -----------------------------------------------------------------------------
def log_heat(value, vmax, floor):
    """Chaleur d'une valeur sur une echelle LOG bornee par [floor, vmax], dans [0, 1].

    heat = (ln(value) - ln(floor)) / (ln(vmax) - ln(floor)), clampe a [0, 1].

    - value <= floor (ou <= 0)  -> 0.0 (froid : negligeable, ou non mesure).
    - value >= vmax             -> 1.0 (le plus chaud).
    - entre les deux            -> position logarithmique.

    Le log evite que main() (le plus gros cumule = vmax = total_tt) ecrase tout en lineaire ; le
    plancher evite l'exces inverse (tout devient tiede), en gardant froid ce qui pese moins que le
    plancher. Cas degeneres (vmax <= floor, floor <= 0) : tout froid, jamais d'exception.
    """
    if floor <= 0 or vmax <= floor or value <= floor or value <= 0:
        return 0.0
    if value >= vmax:
        return 1.0
    return (math.log(value) - math.log(floor)) / (math.log(vmax) - math.log(floor))


# ---- Valeurs a poser sur les lignes de `def`
# -----------------------------------------------------------------------------
def _is_user_path(path):
    """Vrai si `path` est du code UTILISATEUR a colorer.

    Un vrai fichier utilisateur est un source Python (.py). On exclut donc :
      - les fonctions NATIVES/C, que cProfile designe par le pseudo-fichier "~" (piege trouve le
        23/07/2026 : "<built-in method builtins.exec>" a pour cumtime TOUT le run, imports compris,
        et capturait la reference de l'echelle, noyant le code utilisateur) ;
      - les pseudo-noms "<frozen ...>", "<string>", etc. ;
      - la stdlib (/lib/python), site-packages, et la machinerie de kernprof.
    """
    if not path or path.startswith('<') or path == '~':
        return False
    bas = path.replace('\\', '/').lower()
    if not (bas.endswith('.py') or bas.endswith('.pyw') or bas.endswith('.pyx')):
        return False
    return not ('site-packages' in bas or 'dist-packages' in bas
                or '/lib/python' in bas or 'kernprof' in bas)


def user_reference(stats):
    """Haut de l'echelle de chaleur = plus gros cumtime d'une FONCTION du code utilisateur. 0.0 si
    rien.

    On N'utilise PAS total_tt (somme des tottime) : le profilage combine mesure aussi sa PROPRE
    machinerie (imports de kernprof, "eager-preimports", cProfile enveloppant kernprof), qui pese
    lourd et gonfle total_tt. Ancrer l'echelle dessus rendrait le code utilisateur negligeable et
    ETEINDRAIT sa coloration (bug constate le 23/07/2026).

    On exclut aussi le pseudo-"<module>" (le programme entier) et les "<lambda>"/"<listcomp>"...
    (demande de l'utilisateur, 23/07/2026 : le sommet de l'echelle doit etre la FONCTION la plus
    chaude, qui atteint alors l'orange plein - et non le programme entier, qui laisserait meme la
    fonction la plus chaude en-dessous du maximum). Les CLASSES (co_name = nom de classe, sans '<')
    restent comptees mais leur cumtime, en pratique negligeable, n'est jamais le maximum.

    Coherence preservee : def et corps restent normalises sur la MEME reference, donc "ligne
    appelant B" et "def de B" (meme valeur = cumtime de B) gardent la meme couleur.
    """
    vmax = 0.0
    for (fname, _lineno, name), (cumtime, _tt, _nc) in stats.items():
        if not name.startswith('<') and cumtime > vmax and _is_user_path(fname):
            vmax = cumtime
    return vmax


def def_values_for_file(filename, stats):
    """{ligne_du_def: {'cumtime', 'tottime', 'ncalls', 'function'}} pour les fonctions de `filename`.

    La ligne du `def` (co_firstlineno) est donnee DIRECTEMENT par cProfile : aucun source a lire,
    aucun ast. On ne garde que les fonctions du fichier demande. Si deux entrees tombaient sur la
    meme ligne de `def` (ne devrait pas arriver dans un fichier valide), on garde le plus gros
    cumtime.
    """
    key = osp.normcase(osp.abspath(str(filename)))
    result = {}
    for (fname, def_line, funcname), (cumtime, tottime, ncalls) in stats.items():
        if not fname or osp.normcase(osp.abspath(fname)) != key:
            continue
        # Ecarte les code objects qui ne sont PAS des `def` de fonction nommee : "<module>" (code
        # de premier niveau), "<lambda>", "<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>".
        # Leur co_firstlineno tombe n'importe ou (ligne 1, milieu d'expression) : les colorer
        # n'aurait pas de sens. Les CLASSES (co_name = nom de classe, sans '<') sont ecartees a
        # part par le consommateur, qui a la source pour reconnaitre une ligne "class ..." (cf.
        # profile_results.load_results).
        if funcname.startswith('<'):
            continue
        precedent = result.get(def_line)
        if precedent is None or cumtime > precedent['cumtime']:
            result[def_line] = {'cumtime': cumtime, 'tottime': tottime,
                                'ncalls': ncalls, 'function': funcname}
    return result
