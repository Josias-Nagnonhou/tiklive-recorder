# TikLive Recorder

Outil perso pour enregistrer les lives TikTok de A à Z, même sans les regarder.

## Ce qu'il fait

- **Enregistrer un live en cours** : colle le lien (`https://www.tiktok.com/@pseudo/live`), un lien court `vm.tiktok.com/...` ou juste `@pseudo`, puis clique sur « Enregistrer le live ». L'enregistrement continue jusqu'à la fin du live, même si tu fermes la page.
- **Surveiller un compte** : ajoute un `@pseudo`. L'outil vérifie toutes les 60 secondes et lance l'enregistrement tout seul dès que la personne passe en live.
- **Coupures réseau** : si le flux se coupe mais que le live continue, l'outil reprend automatiquement et recolle tous les morceaux en une seule vidéo.
- **À la fin** : un fichier MP4 est disponible via le bouton « Télécharger ».
- **Bouton Arrêter** : il coupe l'enregistrement quand tu veux et garde ce qui a déjà été capté.

## Option A : sur ton ordinateur Windows (pour tester en premier)

1. Dézippe le dossier.
2. Double-clique sur **LANCER-WINDOWS.bat**. La première fois, il installe Python et ffmpeg, puis il faut le relancer.
3. La page s'ouvre sur http://localhost:8000.

Laisse la fenêtre noire ouverte et le PC allumé pendant les lives. Les vidéos sont dans `data/recordings`.

Depuis ton téléphone, sur le même Wi-Fi, ouvre `http://IP-DU-PC:8000`. Pour trouver l'IP du PC, tape `ipconfig` dans un terminal.

## Option B : sur un serveur (Render), qui tourne 24h/24

Le fichier `render.yaml` est prêt (plan Starter + disque de 20 Go).

1. Mets ce dossier dans un dépôt GitHub (privé).
2. Sur Render, choisis New → Blueprint, puis sélectionne le dépôt.
3. Le mot de passe est généré automatiquement. Tu le trouves dans Render → ton service → Environment → `APP_PASSWORD`. À la connexion, l'identifiant peut être n'importe quoi ; seul le mot de passe compte.

Coût indicatif : environ 7 $/mois pour le service, plus environ 0,25 $/Go/mois pour le disque.

## Réglages (variables d'environnement)

| Variable | Rôle |
|---|---|
| `APP_PASSWORD` | Mot de passe d'accès. Vide = pas de mot de passe (OK en local seulement). |
| `WATCH_INTERVAL` | Secondes entre deux vérifications des comptes surveillés (60 par défaut). |
| `TIKTOK_COOKIE` | Optionnel : `sessionid=...` de ton compte TikTok, pour les lives réservés aux connectés ou restreints par âge. |
| `DATA_DIR` | Dossier où sont stockées les vidéos. |

## Si ça ne marche plus

- **« TikTok ne répond pas au serveur »** : TikTok bloque parfois les serveurs cloud. Lancer l'outil sur ton PC (option A) règle presque toujours le problème.
- **Un live existe mais l'outil dit « pas en live »** : TikTok a peut-être changé son site. L'outil met à jour yt-dlp à chaque démarrage ; redémarre-le. Si ça persiste, reviens me voir.
- **Espace disque** : une heure de live pèse environ 1 à 2 Go. Supprime les vidéos après les avoir téléchargées.

## À garder en tête

Enregistrer pour ton usage perso ne pose pas de problème. Si tu republies des extraits du live de quelqu'un, crédite la personne. Le mieux est d'avoir son accord, car le contenu lui appartient et TikTok peut sanctionner les reposts.
