#!/usr/bin/env python3
"""Patch spyder_line_profiler/spyder/widgets.py : allege les DEUX barres du dock "Line Profiler" en
deplaçant six de leurs boutons dans son menu burger (options), jusque-la vide.

Contexte : deuxieme passe du chapitre "Dock2" de CachyOS/Documentation/TODO - Spyder -
cosmetique.txt, arbitrage de l'utilisateur le 26/07/2026.

CONSTAT. Le dock a deux barres : la principale (liste des fichiers, "Open Script", "Profile by
line", "Arreter") et une barre d'information (Tout replier, Tout deplier, la date de la derniere
mesure entre deux etirements, puis Sortie, Enregistrer, Effacer). Son menu burger, lui, etait
entierement VIDE - il ne contenait que les quatre actions de dock ajoutees d'office par
PluginMainWidget. Le dock est masque par defaut sur cette machine (cf. patch_spyder_hide_docks.py :
son tableau par ligne fait doublon avec les timings que le greffon SmartOS peint deja DANS
l'editeur), mais il reste reaffichable par Affichage > Panneaux.

CHANGEMENT. Six boutons passent dans le burger, en deux sections :
  - le pliage de l'arbre (Tout replier, Tout deplier) - l'arbre se plie aussi a la souris ;
  - les actions de sortie et de donnees (Sortie, Enregistrer, Effacer) ;
  - "Open Script" (le bouton parcourir), pour la meme raison que dans le dock Analyse de code : on
    profile presque toujours le fichier courant, et la liste deroulante voisine garde l'historique.
La barre principale se reduit donc a la LISTE et aux deux commandes qui font le travail ("Profile by
line", "Arreter") ; la barre d'information ne garde que la date, centree entre ses deux etirements.

Les actions restent CREEES et enregistrees, y compris le desarmement en bloc quand line_profiler
n'est pas installe (`if not is_lineprofiler_installed()`), qui agit sur les OBJETS et ne depend pas
de leur emplacement : on ne change QUE ou elles s'affichent.

Usage : patch_spyder_lineprofiler_toolbar.py <chemin vers spyder_line_profiler/spyder/widgets.py>

IDEMPOTENCE PAR BLOC : chaque bloc porte SON marqueur et est saute s'il est deja en place, de sorte
qu'une passe ulterieure puisse ajouter un bloc sans rien defaire. Re-parse avant ecriture, echec
BRUYANT (code 1) si un bloc est introuvable ou non unique - jamais deviner.
"""
import ast
import sys

# (marqueur, ancien, nouveau), appliques DANS CET ORDRE.
PAIRS = [
    (
        "le menu burger de ce dock etait vide",
        '''        # Main Toolbar
        toolbar = self.get_main_toolbar()
        for item in [self.filecombo, self.browse_action, self.start_action,
                     self.stop_action]:
            self.add_item_to_toolbar(
                item,
                toolbar=toolbar,
                section=SpyderLineProfilerWidgetMainToolbarSections.Main,
            )

        # Secondary Toolbar
        secondary_toolbar = self.create_toolbar(
            SpyderLineProfilerWidgetToolbars.Information)
        for item in [self.collapse_action, self.expand_action,
                     self.create_stretcher(
                         id_=SpyderLineProfilerWidgetInformationToolbarItems.Stretcher1),
                     self.datelabel,
                     self.create_stretcher(
                         id_=SpyderLineProfilerWidgetInformationToolbarItems.Stretcher2),
                     self.log_action,
                     self.save_action,
                     self.clear_action]:
            self.add_item_to_toolbar(
                item,
                toolbar=secondary_toolbar,
                section=SpyderLineProfilerWidgetInformationToolbarSections.Main,
            )''',
        '''        # SmartOS (patch_spyder_lineprofiler_toolbar.py) : le menu burger de ce dock etait vide
        # - il ne contenait que les quatre actions de dock ajoutees d'office par PluginMainWidget.
        # Il recoit ici le pliage de l'arbre, les actions de sortie et de donnees, et le bouton
        # "parcourir" ; les deux barres se reduisent d'autant.
        # SmartOS : libelles FRANCAIS explicites. Ce greffon n'a AUCUN catalogue de traduction
        # (Spyder le journalise au demarrage : "No translation file found for domain
        # 'spyder_line_profiler.spyder'"), et ses libelles ne se voyaient pas jusqu'ici - boutons a
        # icone seule, le texte ne servant qu'a l'infobulle. Dans un menu ils se LISENT, et ils
        # melangeaient anglais, francais et une coquille amont ("Collaps"). On ne passe pas par _()
        # : sans catalogue, il rendrait la chaine inchangee.
        for item, libelle in [
            (self.collapse_action, "Tout replier"),
            (self.expand_action, "Tout déplier"),
            (self.log_action, "Sortie complète"),
            (self.clear_action, "Effacer la sortie"),
            (self.browse_action, "Sélectionner un fichier Python"),
        ]:
            item.setText(libelle)

        options_menu = self.get_options_menu()
        for item in [self.collapse_action, self.expand_action]:
            self.add_item_to_menu(
                item, menu=options_menu, section="smartos_lp_tree"
            )
        for item in [self.log_action, self.save_action, self.clear_action,
                     self.browse_action]:
            self.add_item_to_menu(
                item, menu=options_menu, section="smartos_lp_data"
            )

        # Main Toolbar
        # SmartOS : la liste des fichiers et les deux commandes qui font le travail. "Open Script"
        # (parcourir) est dans le menu burger : on profile presque toujours le fichier courant, et
        # la liste deroulante garde de toute facon l'historique.
        toolbar = self.get_main_toolbar()
        for item in [self.filecombo, self.start_action,
                     self.stop_action]:
            self.add_item_to_toolbar(
                item,
                toolbar=toolbar,
                section=SpyderLineProfilerWidgetMainToolbarSections.Main,
            )

        # Secondary Toolbar
        # SmartOS : reduite a la date de la derniere mesure, centree entre ses deux etirements.
        secondary_toolbar = self.create_toolbar(
            SpyderLineProfilerWidgetToolbars.Information)
        for item in [self.create_stretcher(
                         id_=SpyderLineProfilerWidgetInformationToolbarItems.Stretcher1),
                     self.datelabel,
                     self.create_stretcher(
                         id_=SpyderLineProfilerWidgetInformationToolbarItems.Stretcher2)]:
            self.add_item_to_toolbar(
                item,
                toolbar=secondary_toolbar,
                section=SpyderLineProfilerWidgetInformationToolbarSections.Main,
            )''',
    ),
]


def appliquer(source, path, nom, pairs):
    """Applique les blocs dans l'ordre. Renvoie (source_patchee, nb_appliques) ou (None, 0)."""
    applied = 0
    for marqueur, olds, new in pairs:
        if marqueur in source:
            continue  # bloc deja en place
        if isinstance(olds, str):
            olds = (olds,)
        # PREMIER candidat qui matche exactement une fois, les candidats etant donnes DU PLUS
        # SPECIFIQUE AU PLUS GENERAL. Ce n'est pas un detail : le texte pose par une passe
        # precedente CONTIENT en general le texte d'origine de Spyder (elle n'avait fait qu'y
        # ajouter des lignes), donc les deux matchent, et exiger un candidat unique echouerait.
        trouve = next((o for o in olds if source.count(o) == 1), None)
        if trouve is None:
            print(f"Aucun des {len(olds)} textes attendus n'est present exactement une fois dans "
                  f"{path} - le code amont a peut-etre ete restructure, patch {nom} non applique. "
                  f"Bloc:\n{olds[0][:80]}...", file=sys.stderr)
            return None, 0
        source = source.replace(trouve, new)
        applied += 1
    return source, applied


def main():
    if len(sys.argv) != 2:
        print(f"Usage : {sys.argv[0]} <chemin vers spyder_line_profiler/spyder/widgets.py>",
              file=sys.stderr)
        return 1

    path = sys.argv[1]
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
    except OSError as error:
        print(f"widgets.py illisible ({error}) - patch barres Line Profiler non applique.",
              file=sys.stderr)
        return 1

    patched, applied = appliquer(source, path, "barres Line Profiler", PAIRS)
    if patched is None:
        return 1
    if applied == 0:
        print("Patch barres Line Profiler : tous les blocs sont deja en place.")
        return 0

    try:
        ast.parse(patched)
    except SyntaxError as error:
        print(f"Le widgets.py patche n'est pas du Python valide ({error}) - aucune modification "
              "ecrite.", file=sys.stderr)
        return 1

    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    print(f"Patch barres Line Profiler applique ({applied} bloc(s)) : six boutons deplaces dans le "
          f"menu burger ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
