/**
 * Collection edits. Listing, creating, deleting and membership live in
 * client.ts; renaming and describing a collection (PATCH, audited as
 * COLLECTION_UPDATE on the server) had no client call at all.
 */

import { request } from "./client";
import type { Collection } from "./types";

export const collectionsApi = {
  /** PATCH {name?, description?}; any other field is a 400. */
  update: (id: number, changes: { name?: string; description?: string }) =>
    request<Collection>(`/api/v1/collections/${id}/`, { method: "PATCH", body: changes }),
};
