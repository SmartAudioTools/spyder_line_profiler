# -*- coding: utf-8 -*-
"""Mode « Σ » du Line Profiler : lecture et mise en forme des operations elementaires comptees.

Module PUR (ni Qt ni Spyder), teste a part : il lit le JSON ecrit par cx_launcher.py et rend des
objets prets a peindre - comptes par ligne, instructions qui cachent des operations, chaleur de
chacune. profile_results.py s'en sert pour la marge de droite, le fond des lignes et le
soulignement des instructions cachees.

Mise en forme REPRISE de l'editeur de QCM de SmartTeacher (QCM/editeur_code.py, fonctions
`nombre`, `chaleur`, `etiquette` et l'infobulle des instructions cachees), pour que les deux outils
ecrivent les memes nombres de la meme facon : "2·10⁸" au-dela du million, virgule decimale.
"""

import json
import math
import os.path as osp

EXPOSANTS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def nombre(n):
    """Nombre court : entier tel quel sous le million, puis "mantisse·10ᵖ" (comme SmartTeacher)."""
    if n is None:
        return ''
    if n < 10 ** 6:
        if n != int(n):
            return f"{n:.1f}".rstrip("0").rstrip(".").replace(".", ",")
        return str(int(n))
    puissance = math.floor(math.log10(n))
    mantisse = f"{n / 10 ** puissance:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return ("" if mantisse == "1" else mantisse + "·") + "10" + str(puissance).translate(EXPOSANTS)


def chaleur(operations, plus):
    """Position sur l'echelle thermique, LOGARITHMIQUE : les comptes s'etalent sur des ordres de
    grandeur, une echelle lineaire laisserait tout dans le froid sauf la pire ligne."""
    if plus > 1 and operations > 1:
        return min(1.0, math.log(operations) / math.log(plus))
    return 0.0


class LigneComptee:
    """Compte d'une ligne : passages, operations elementaires, chaleur (pour le fond)."""

    __slots__ = ('passages', 'operations', 'heat')

    def __init__(self, passages, operations, heat=0.0):
        self.passages = passages
        self.operations = operations
        self.heat = heat

    def segments(self):
        """Les cinq morceaux de la formule, alignes sur ceux du temps :
        passages, ×, operations par passage, =, operations totales."""
        if not self.passages:
            return ('0', '×', '…', '=', total(self.operations))
        return (nombre(self.passages), '×', moyenne(self.operations, self.passages) + ' op',
                '=', total(self.operations))

    def total(self):
        return total(self.operations)


def moyenne(operations, passages):
    """Operations par passage, ecrites pour que le produit se verifie a l'oeil (meme regle que
    l'editeur de QCM, EditeurCode.etiquette, alignee le 30/10/2026 a la demande de SmartTeacher) :
    exacte quand elle tient en trois decimales (« 1000 × 499,501 = 499501 », et non 499,5),
    sinon arrondie et precedee de « ~ » (« 3 × ~0,3 = 1 ») ; au-dela du million, "1,4·10⁸".
    « ~ » et non le « ≈ » de l'editeur de QCM : dans la petite police de la marge, « ≈ » se lit
    comme « = » (retour de l'utilisateur, 30/10/2026)."""
    valeur = operations / passages
    if valeur >= 10 ** 6:
        return nombre(valeur)
    decimales = next((d for d in range(4) if operations * 10 ** d % passages == 0), None)
    if decimales is None:
        return "~" + nombre(valeur)
    return f"{valeur:.{decimales}f}".replace(".", ",")


def total(operations):
    return nombre(operations) + ' op'


class InstructionCachee:
    """Instruction qui cache des operations (pop(0), x in liste...), a souligner dans l'editeur.

    Lignes en base 1, colonnes en CARACTERES et fin exclusive (convention de ast, que le moteur
    reprend) - pret pour block.position() + colonne.
    """

    __slots__ = ('ligne', 'colonne', 'fin_ligne', 'fin_colonne', 'texte', 'passages',
                 'operations', 'nature', 'heat')

    def __init__(self, ligne, colonne, fin_ligne, fin_colonne, texte, passages, operations,
                 nature, heat=0.0):
        self.ligne = ligne
        self.colonne = colonne
        self.fin_ligne = fin_ligne
        self.fin_colonne = fin_colonne
        self.texte = texte
        self.passages = passages
        self.operations = operations
        self.nature = nature
        self.heat = heat

    def infobulle(self):
        """Meme texte que l'editeur de QCM : ce que l'instruction cache, et ce que ca coute."""
        texte = (f"{self.texte} : {self.nature}\n"
                 f"{nombre(self.operations)} opérations élémentaires")
        if self.passages > 1:
            texte += f", sur {nombre(self.passages)} passages"
        return texte

    def contient(self, ligne, colonne):
        """Vrai si la position (ligne base 1, colonne) tombe dans l'instruction."""
        if not self.ligne <= ligne <= self.fin_ligne:
            return False
        if ligne == self.ligne and colonne < self.colonne:
            return False
        if ligne == self.fin_ligne and colonne >= self.fin_colonne:
            return False
        return True


def lire(path):
    """Lit le JSON de cx_launcher. Renvoie None s'il est absent ou illisible, sinon :
    {'fichier': chemin normalise, 'source': texte compte, 'lignes': {n: LigneComptee},
     'caches': [InstructionCachee], 'hors_script': [...], 'erreur': str|None, 'total': n,
     'fonctions': {ligne du def: LigneComptee}} - passages = appels, pour la ligne du `def`.

    La chaleur est ramenee a la ligne la plus couteuse du compte (meme echelle pour les fonds et
    les soulignements) : une instruction cachee a donc la couleur qu'aurait une ligne de son cout.
    """
    try:
        with open(path, 'r', encoding='utf-8') as flux:
            donnees = json.load(flux)
    except (OSError, ValueError):
        return None
    if not isinstance(donnees, dict) or not donnees.get('fichier'):
        return None
    brutes = {int(n): v for n, v in (donnees.get('lignes') or {}).items()}
    plus = max((ops for _p, ops in brutes.values()), default=0)
    lignes = {n: LigneComptee(p, ops, chaleur(ops, plus)) for n, (p, ops) in brutes.items()}
    caches = []
    for ligne, col, fin_ligne, fin_col, texte, passages, ops, nature in donnees.get('caches') or []:
        caches.append(InstructionCachee(ligne, col, fin_ligne, fin_col, texte, passages, ops,
                                        nature, chaleur(ops, plus)))
    # Le `def` sur la MEME echelle que les lignes, comme son cumule cProfile en mode horloge : son
    # total depasse la pire ligne, il sature donc au plus chaud (« la ligne de definition de la
    # fonction n'est pas coloree comme avec le line profiler », l'utilisateur, 30/10/2026).
    fonctions = {int(n): LigneComptee(appels, ops, chaleur(ops, plus))
                 for n, (appels, ops) in (donnees.get('fonctions') or {}).items()}
    erreur = donnees.get('erreur')
    return {'fonctions': fonctions, 'fichier': osp.normcase(donnees['fichier']), 'source': donnees.get('source'),
            'lignes': lignes, 'caches': caches, 'hors_script': donnees.get('hors_script') or [],
            'erreur': None if erreur is None else str(erreur), 'total': donnees.get('total')}
