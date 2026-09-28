# Module KDS

Le module KDS gere les ecrans de preparation, les codes d'association temporaires et les sessions
remote utilisees par l'app admin/staff.

## Ecrans remote

- `kds_screens.remote_enabled` indique explicitement si un ecran peut etre pilote par remote.
- Un ecran inactif ou `remote_enabled=false` ne doit pas produire de QR ni de code d'association.
- Les ecrans Service, Cuisine et Comptoir restent filtres cote app selon leur mode/station, mais la
  validation finale est toujours cote backend.

## QR d'association

- `POST /kds/screens/{screen_id}/pairing-code` requiert `orders:preparation`.
- La reponse contient toujours le code 6 chiffres temporaire, son expiration et `pairing_payload`.
- `pairing_payload` est chiffre et authentifie avec Fernet. Il contient le tenant, l'ecran, le code,
  l'expiration et un nonce. Le QR ne doit pas exposer le code en clair.
- La creation d'un nouveau code invalide les anciens codes non utilises et non expires du meme ecran.

## Resolution mobile

- `POST /kds/pairing-payload/resolve` requiert aussi `orders:preparation`.
- Le endpoint refuse un payload invalide, modifie, expire, d'un autre tenant, d'un ecran inactif ou
  d'un ecran dont le remote est desactive.
- L'app mobile doit etre connectee avant le scan QR dans cette premiere version.
- La resolution retourne le code 6 chiffres pour pre-remplir `/kitchen/remote`; l'association finale
  passe toujours par `POST /kds/pair`.

## Securite et configuration

- `KDS_QR_ENCRYPTION_KEY` peut etre fourni pour isoler le chiffrement des QR remote.
- Si la variable est vide, la cle est derivee du secret HMAC/JWT existant pour garder un deploiement
  compatible, mais une cle dediee est recommandee en production.
- `POST /kds/pair` conserve son rate limit et consomme le code a usage unique.
- Les permissions restent staff/admin avec `orders:preparation`; un profil lecture seule ne doit pas
  generer, resoudre ni associer de remote.

## Exploitation

- Les QR sont prevus pour des ecrans desktop/tablette deployes avec l'app admin/staff, par exemple sur
  Netlify.
- Pour que les QR contiennent une URL absolue cote frontend, definir `APP_PUBLIC_URL` dans le build
  admin/staff.
- Les deep links natifs iOS/Android ne font pas partie de cette version : le flux mobile passe par le
  scanner integre a l'app connectee.
