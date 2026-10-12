# Mise en production — HACCP phase 5

## Fonctionnement

Le worker ARQ exécute le rappel toutes les 15 minutes. Pour chaque établissement strictement
activé, il calcule l'ouverture dans le fuseau local et couvre H−60, H−45, H−30, H−15 et H.
Les destinataires sont les admins actifs et les membres staff habilités de l'établissement.

Les livraisons sont uniques par établissement, date métier, révision d'horaire, échéance,
destinataire et canal. Une reprise après arrêt ne rejoue pas en rafale les échéances trop anciennes.
Une journée déjà validée ne génère aucun nouveau rappel.

## Déploiement

1. Déployer l'API et le worker avec la même révision applicative.
2. Vérifier Redis ARQ/pub-sub et les credentials APNs/FCM ; une panne de notification ne doit
   jamais rendre l'ouverture ou la commande possible.
3. Vérifier le cron ARQ à `:00`, `:15`, `:30` et `:45`.
4. Sur staging, configurer une ouverture à quelques minutes et vérifier les statuts `pending`,
   `sending`, `sent` ou `failed` dans `haccp_reminder_deliveries`.
5. Valider un admin, un staff habilité et un staff d'un autre établissement ; seul le bon périmètre
   doit recevoir l'alerte.

## Exploitation et retour arrière

Surveiller les rappels `failed`, les retards du worker, les erreurs Redis et les fournisseurs push.
Le retour applicatif peut désactiver `strict_enabled` ou arrêter les rappels sans supprimer leur
historique. Ne pas supprimer les lignes de livraison ni contourner le garde HACCP pour compenser
une panne de notification.
