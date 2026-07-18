// Test de charge basique (k6) -- api-pizza
//
// [PERF] Couvre les 3 endpoints les plus critiques identifies par l'audit
// performance : GET /catalog/products (impacte par le N+1 desormais corrige
// + le cache Redis ajoute), POST /orders, GET /orders. Objectif : etablir une
// baseline de latence, pas un test de rupture -- ajuster VUS/DURATION pour
// aller plus loin.
//
// Usage :
//   k6 run scripts/load-test.js
//   k6 run -e BASE_URL=https://staging.example.com scripts/load-test.js
//
// Variables d'environnement :
//   BASE_URL     URL de base de l'API (defaut: http://localhost:8000)
//   TENANT_SLUG  Slug d'un tenant existant avec au moins un produit actif
//                (defaut: loadtest -- voir setup() qui le cree si absent)
//
// Le script cree son propre tenant/produit de test dans setup() pour rester
// executable de bout en bout sans dependre d'un jeu de donnees pre-existant.

import http from "k6/http";
import { check, sleep } from "k6";
import { Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const TENANT_SLUG = __ENV.TENANT_SLUG || `loadtest${Date.now()}`;

const catalogLatency = new Trend("catalog_listing_duration");
const orderCreateLatency = new Trend("order_create_duration");
const orderListLatency = new Trend("order_list_duration");

export const options = {
  scenarios: {
    catalog_browsing: {
      executor: "constant-vus",
      exec: "browseCatalog",
      vus: 20,
      duration: "1m",
    },
    order_flow: {
      executor: "constant-vus",
      exec: "createAndListOrders",
      vus: 5,
      duration: "1m",
    },
  },
  thresholds: {
    // Baseline indicative -- a resserrer une fois une premiere mesure reelle obtenue.
    catalog_listing_duration: ["p(95)<500"],
    order_create_duration: ["p(95)<800"],
    order_list_duration: ["p(95)<500"],
    http_req_failed: ["rate<0.01"],
  },
};

export function setup() {
  const email = `loadtest-${Date.now()}@example.com`;
  const register = http.post(
    `${BASE_URL}/api/v1/auth/register`,
    JSON.stringify({
      tenant_slug: TENANT_SLUG,
      tenant_name: "Load Test Tenant",
      email,
      password: "Valid1!aa",
    }),
    { headers: { "Content-Type": "application/json" } }
  );
  check(register, { "setup: tenant registered": (r) => r.status === 201 });
  const adminToken = register.json("access_token");
  const authHeaders = {
    Authorization: `Bearer ${adminToken}`,
    "X-Tenant-Slug": TENANT_SLUG,
    "Content-Type": "application/json",
  };

  const category = http.post(
    `${BASE_URL}/api/v1/catalog/categories`,
    JSON.stringify({ name: "Load Test Category" }),
    { headers: authHeaders }
  );
  const categoryId = category.json("id");

  const product = http.post(
    `${BASE_URL}/api/v1/catalog/products`,
    JSON.stringify({
      name: "Load Test Pizza",
      description: "Produit cree pour le test de charge",
      base_price: 9.9,
      category_id: categoryId,
      is_active: true,
    }),
    { headers: authHeaders }
  );
  const productId = product.json("id");

  return { adminToken, productId, tenantSlug: TENANT_SLUG };
}

export function browseCatalog(data) {
  const res = http.get(
    `${BASE_URL}/api/v1/catalog/products?page=1&page_size=50`,
    { headers: { "X-Tenant-Slug": data.tenantSlug } }
  );
  catalogLatency.add(res.timings.duration);
  check(res, { "catalog: 200": (r) => r.status === 200 });
  sleep(1);
}

export function createAndListOrders(data) {
  const headers = {
    Authorization: `Bearer ${data.adminToken}`,
    "X-Tenant-Slug": data.tenantSlug,
    "Content-Type": "application/json",
  };

  const createRes = http.post(
    `${BASE_URL}/api/v1/orders`,
    JSON.stringify({
      items: [{ product_id: data.productId, quantity: 1, extras: [] }],
    }),
    { headers: { ...headers, "Idempotency-Key": `loadtest-${__VU}-${__ITER}-${Date.now()}` } }
  );
  orderCreateLatency.add(createRes.timings.duration);
  check(createRes, { "order create: 201": (r) => r.status === 201 });

  const listRes = http.get(`${BASE_URL}/api/v1/orders?page=1&page_size=20`, { headers });
  orderListLatency.add(listRes.timings.duration);
  check(listRes, { "order list: 200": (r) => r.status === 200 });

  sleep(1);
}
