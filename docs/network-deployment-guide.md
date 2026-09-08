# Guide d'installation réseau — déploiement Kitchen en restaurant

Public : technicien qui câble/configure le réseau chez un client (restaurant indépendant ou petit
groupe), au moment du déploiement Kitchen. Objectif : éviter qu'un Cloud correctement sécurisé
(TLS, JWT, isolation par schéma — voir `CLAUDE.md`) soit contourné par un Wi-Fi local ouvert ou mal
séparé. Ceci est un guide d'installation, pas un runbook d'incident — pour les procédures
opérationnelles côté API, voir [RUNBOOK.md](../RUNBOOK.md).

## 1. Principe

Les flux métier principaux (tablettes, KDS, caisse) passent par HTTPS vers l'API Kitchen dans le
Cloud — aucun serveur Kitchen local ni communication LAN généralisée entre ces appareils n'est
requis. Ça simplifie beaucoup la topologie par rapport à un système de caisse legacy on-prem : pas
de serveur local applicatif à protéger, seulement un **point de sortie Internet** à sécuriser et
des **appareils à cloisonner entre eux**.

Nuance importante : si l'impression réseau locale est activée (tablette/caisse → imprimante de
commande via IP locale plutôt que via un module d'impression cloud), ce flux-là **est** local et
doit être explicitement autorisé — voir section 2. Il doit rester limité à des imprimantes
désignées, avec IP/ports explicitement autorisés, et n'est jamais accessible depuis le Wi-Fi
invités.

La segmentation ci-dessous répond à une seule question par zone : *si cet appareil est compromis,
qu'est-ce qu'il peut atteindre ?*

## 2. Zones

| Zone | Équipements | Accès autorisés | Accès bloqués |
|---|---|---|---|
| **Wi-Fi invités** | Téléphones et tablettes des clients | Internet | Réseau métier, imprimantes, interface d'administration du routeur |
| **Métier / Staff & cuisine** | Tablettes de prise de commande, KDS, caisse, imprimantes de commande | API Kitchen (Internet, HTTPS), imprimantes autorisées, services métier locaux (si présents) | Réseau invités, administration réseau, TPE |
| **Paiement** | TPE, passerelle de paiement | Prestataires de paiement nécessaires uniquement (acquéreur bancaire, Stripe Terminal, etc.) | Réseau invités **et** accès libre depuis le réseau Staff |
| **Administration** | Routeur, firewall, switches manageables | Poste administrateur désigné uniquement | Tout le reste (pas d'accès direct depuis invités, staff ou paiement) |

Points qui ne sont pas dans le tableau mais qui font échouer un déploiement en pratique :

- **Le Wi-Fi invités doit avoir l'isolation client (AP/client isolation) activée**, pas seulement
  un SSID différent. Sans ça, deux téléphones sur le même Wi-Fi invités se voient entre eux — un
  SSID séparé ne suffit pas à lui seul si l'AP autorise le trafic inter-clients.
- **Le réseau Staff n'a pas besoin d'accès entrant depuis Internet.** Les tablettes/KDS ne font que
  des requêtes sortantes vers l'API Kitchen ; aucun port ne doit être ouvert en entrée sur le
  routeur pour ces appareils. [🔒 SÉCURITÉ]
- **Le TPE ne doit pas être joignable depuis le Wi-Fi staff**, même si c'est pratique pour le
  debug. C'est le sens de "accès bloqués : accès libre depuis Staff" dans le tableau — le trafic
  carte bancaire n'a rien à faire visible depuis un réseau où un téléphone perso peut se connecter
  par erreur. Le VLAN/réseau paiement reste **fermé par défaut** depuis Staff. Si l'intégration du
  terminal impose une communication locale (ex. caisse qui pilote le TPE en direct plutôt que via
  la passerelle cloud du prestataire), n'autoriser que le flux précis **caisse désignée → TPE
  désigné**, sur les ports documentés par le prestataire — jamais une ouverture générale
  Staff → Paiement.
- **Si l'impression réseau locale est utilisée**, le flux tablette/caisse → imprimante doit être
  explicitement autorisé (règle firewall dédiée, pas une ouverture large du VLAN staff), limité aux
  IP/ports des imprimantes désignées, et jamais accessible depuis le Wi-Fi invités.

## 3. Quand 3 réseaux suffisent, quand il en faut plus

Trois zones **métier** séparées — invités / staff-cuisine / paiement — au niveau **SSID Wi-Fi avec
isolation client + VLAN dédié au moins pour le paiement** couvrent la grande majorité des
installations Kitchen. L'Administration (section 2) est une zone de gestion à part : elle existe
dès que le matériel le permet (routeur/switch manageables) ou qu'un administrateur désigné est
identifié, indépendamment du nombre de zones métier — ce n'est pas une quatrième zone "au même
niveau" que les trois autres, c'est le plan de contrôle qui les administre toutes. Ne pas
sur-designer avec six VLAN "au cas où" — ça complique la maintenance pour un gain de sécurité nul
si le contexte ne le justifie pas.

**3 zones suffisent quand :**
- Un seul site, une seule caisse/back-office.
- Tous les appareils métier sont Cloud (API Kitchen), aucun serveur de fichiers, NAS ou logiciel
  legacy local à protéger.
- Le TPE se connecte à l'acquéreur directement (4G/eSIM intégrée au terminal, ou VLAN paiement
  dédié avec règles firewall strictes) sans dépendre du reste du LAN pour autre chose que la sortie
  Internet.
- Pas d'obligation contractuelle de conformité PCI DSS documentée avec ta banque acquéreuse
  au-delà des règles standard (la majorité des petits indépendants sont sur un SAQ simplifié où
  la ségrégation logique suffit).

**Une segmentation VLAN supplémentaire est justifiée quand :**
- **Groupe multi-sites** : chaque restaurant doit avoir son propre sous-réseau/VLAN, avec un VLAN
  management séparé pour l'admin centralisée (VPN site-à-site ou SD-WAN) — sinon un incident sur
  un site expose la topologie des autres.
- **Objets connectés annexes** (caméras de vidéosurveillance, alarme, badgeuse, capteurs
  température frigo) : ce sont typiquement les appareils avec le firmware le moins bien maintenu
  du bâtiment. Ils doivent avoir leur propre VLAN, jamais partager le broadcast domain avec les
  tablettes/KDS. [🔒 SÉCURITÉ]
- **Back-office / compta** avec un poste qui manipule des données sensibles (exports comptables,
  accès admin Kitchen élevé) : séparer ce poste du Wi-Fi staff que tout le personnel de salle
  utilise.
- **Exigence PCI DSS explicite de l'acquéreur/banque** : dans ce cas la segmentation doit être
  un VLAN documenté avec règles firewall traçables (pas juste un SSID), parce que l'auditeur PCI
  demande une preuve de cloisonnement réseau, pas une confiance sur la config du point d'accès.

Dans le doute, la règle simple : trois zones métier logiques minimum toujours (+ Administration dès
que le matériel/un responsable le permet) ; VLAN dédié en plus dès qu'un appareil du bâtiment n'est
*pas* sous le contrôle direct de l'exploitant (multi-site, prestataire tiers, IoT) ou dès qu'une
exigence contractuelle l'impose.

## 4. Connexion Internet

- **Ligne principale** : fibre ou ADSL pro dédiée au restaurant, pas une box résidentielle
  partagée avec le logement du gérant.
- **Secours 4G/5G** : routeur avec failover automatique (double WAN, ou clé/routeur 4G en
  bascule). [⚠️ PROD] Sans ça, une coupure fibre bloque la prise de commande *et* l'encaissement
  simultanément — c'est le point de panne le plus visible en salle, à tester à l'installation
  (débrancher la ligne principale et vérifier la bascule effective, pas juste sur le papier).
- Le failover doit couvrir **staff** et **paiement** en priorité ; le Wi-Fi invités peut rester
  dégradé/coupé en secours si la bande passante 4G est limitée — ce n'est pas ce qui bloque le
  service.

## 5. Mots de passe Wi-Fi

- **Un mot de passe par SSID, jamais partagé entre zones.** Le mot de passe du Wi-Fi staff ne doit
  jamais être le même que celui du Wi-Fi invités, et ne doit jamais être affiché en salle (là où
  le mot de passe invités, lui, est fait pour être visible/communiqué).
- WPA2-PSK minimum, WPA3 si le matériel le supporte pour la zone staff et la zone paiement.
- Rotation : à chaque départ d'un membre du staff qui avait le mot de passe (le SSID staff n'a pas
  d'identifiants nominatifs, donc le seul levier de révocation est de changer le mot de passe
  partagé), et au minimum une fois par an sinon.
- Le mot de passe admin du routeur est **distinct** des mots de passe Wi-Fi et n'est connu que de
  l'administrateur désigné (zone Administration, section 2).

## 6. Mises à jour du routeur

- Mises à jour firmware automatiques quand le matériel le permet ; sinon vérification manuelle
  planifiée (a minima trimestrielle) — un routeur avec un firmware daté de plusieurs années est
  une des portes d'entrée les plus communes en pentest réseau local.
- Désactiver WPS et l'administration à distance depuis le WAN (le routeur ne doit être
  administrable que depuis la zone Administration en local, ou via VPN si gestion centralisée
  multi-sites). [🔒 SÉCURITÉ]
- Changer les identifiants admin par défaut du routeur/switch avant la mise en service — évident,
  mais c'est la cause n°1 de prise de contrôle d'un routeur SoHo laissé en confiration usine.

## 7. Inventaire des appareils

Tenir une liste simple (tableur suffit, pas besoin d'outil dédié pour un indépendant) avec au
minimum :

| Appareil | Zone | Adresse MAC | Attribué à | Date de mise en service | Statut |
|---|---|---|---|---|---|
| Tablette prise de commande #1 | Staff | `AA:BB:CC:...` | Salle | 2026-09-01 | Actif |
| TPE #1 | Paiement | `AA:BB:CC:...` | Caisse | 2026-09-01 | Actif |

Utilité concrète : sans cet inventaire, la procédure "tablette perdue" ci-dessous ne peut pas
identifier rapidement quel appareil bloquer sur le Wi-Fi ni quelle session applicative révoquer.

## 8. Procédure — tablette (ou tout appareil staff) perdue ou volée

Diagnostic → correction → prévention, dans l'ordre, dès la découverte de la perte :

1. **Cause probable** : l'appareil a une session Kitchen active (JWT + refresh token) et est
   toujours connecté au Wi-Fi staff — les deux sont exploitables tant qu'ils ne sont pas traités,
   indépendamment l'un de l'autre.
2. **Correction immédiate** — dans cet ordre de priorité :
   - **Révocation Kitchen (prioritaire)** : `GET /api/v1/auth/sessions` pour identifier la session
     via `user_agent`/`ip_address` de l'appareil perdu, puis `DELETE /api/v1/auth/sessions/{id}`.
     Ça révoque le refresh token de cette session en base — **mais ne révoque pas instantanément
     l'access token déjà émis** : celui-ci reste valide jusqu'à son expiration naturelle (durée de
     vie courte configurée côté API, voir `docs/modules/auth.md`). C'est un risque **borné dans le
     temps**, pas éliminé à l'instant de l'appel.
     Si l'urgence l'exige (vol plutôt que simple oubli, ou identifiant partagé entre plusieurs
     membres du staff), utiliser `DELETE /api/v1/auth/sessions?revoke_current=true` ou désactiver
     directement le compte : ça peut mettre en deny-list le JTI du token courant et déclencher la
     fermeture de connexions actives (WebSocket), ce qui réduit la fenêtre de risque plus vite
     qu'une simple révocation de session ciblée. Ne pas présenter la révocation de session comme
     une coupure d'accès immédiate et totale — c'est une révocation du refresh token avec un délai
     résiduel côté access token.
   - **Retrait des identifiants Wi-Fi** : si l'appareil connaissait un mot de passe Wi-Fi staff
     nominatif ou partagé exposé par la perte, le changer (section 5).
   - **MDM / effacement à distance** si l'appareil est enrôlé dans une solution de gestion — c'est
     le seul mécanisme qui agit sur l'appareil lui-même plutôt que sur ses accès réseau/applicatifs.
   - **Blocage MAC en complément, jamais en mesure principale** : filtrer l'adresse MAC de
     l'appareil sur le point d'accès (en s'appuyant sur l'inventaire, section 7) reste utile
     opérationnellement, mais ce n'est **pas une mesure fiable** — une adresse MAC se spoof
     facilement, et rien n'empêche l'appareil de rejoindre le réseau par un port Ethernet ou de
     sortir directement via le réseau mobile (4G/5G du téléphone) sans passer par le Wi-Fi staff
     du tout. Le blocage MAC ne remplace jamais la révocation applicative ci-dessus.
3. **Prévention future** :
   - Si le volume de matériel le justifie, passer sur une solution MDM (verrouillage/effacement
     à distance) plutôt que de dépendre uniquement de la révocation applicative.
   - Vérifier après coup les logs d'audit des connexions (`docs/modules/auth.md`, section audit)
     pour s'assurer qu'aucune activité suspecte n'a eu lieu entre la perte effective et la
     révocation.
   - Remplacer l'appareil et le réenregistrer dans l'inventaire (section 7) avec une nouvelle
     entrée, plutôt que de recycler l'ancienne ligne.

## Alternative envisagée

Une alternative plus simple aurait été de ne documenter que deux zones (invités vs "tout le
reste"). Trade-off : plus rapide à mettre en place, mais le TPE se retrouve alors visible depuis
n'importe quel appareil staff compromis (téléphone perso connecté par erreur, tablette infectée) —
inacceptable dès qu'il y a un flux carte bancaire, même sans obligation PCI DSS formelle côté
indépendant. D'où les 3 zones minimum de la section 3 plutôt que 2.
