# Caspian — "Consumption Location" spec and integration brief

**For:** the session working on Caspian Warehouse (desktop) and Caspian Owner (Android).
**From:** the building-maps session (October 2026).
**Attached data file:** `caspian_locations.csv` (UTF-8 with BOM, 141 rows).

This document has three parts:

1. **What was decided** about the building and the "consumption location" field.
2. **The spec** for implementing it in Caspian Warehouse, written so it can be handed to Claude Code in Plan mode.
3. **How it fits the bigger system**: Warehouse ↔ middle server ↔ Caspian Owner. The server does not exist yet. The owner (Alireza) has no server experience, so it will be designed and built **step by step together**. This part lists what the location feature needs from the server, not a finished server design.

---

## Part 1 — Decisions from the maps session

### The building
- The Caspian building has two phases. **Only Phase 2 is in scope.** Phase 1 is ignored entirely.
- Phase 2 floors (bottom to top): parking **P4, P3, P2, P1**, **basement** (زیرهمکف), **ground** (همکف), **1st** (اول), **2nd** (دوم), **3rd** (سوم), **roof** (بام).
- Shop plates (پلاک) in Phase 2:

| Floor | Plates |
|---|---|
| Basement | 7–45 |
| Ground | 58–82 |
| 1st | 93–121 |
| 2nd | 122–135, plus 126/1 |
| 3rd | 136–149 (the whole floor is leased to a single carpet tenant) |

- **Phase 1 plates:** 1–6, 46–57 and 83–92. These must be rejected with a clear message.
- **Merged shops:** some plates are one shop, e.g. 35-36, 94-95, 96/97, 101/108, 102/107, 103/106, 104/105, 109/110, 113-114, 116/117 and 118/119/120/121. This is informational only. Every number stays individually selectable.

### Why the field is simple
- Almost all warehouse consumption happens in **common areas** (corridors, stairs, elevators, parking), not inside shops. The plate is just an **address** ("in front of plate 68").
- **For now the only level of detail is the floor.** An earlier idea was to split each floor into halves or zones. It was **explicitly postponed** until there is real user feedback. The data model must allow adding it later without breaking history.
- Shop names are **not** stored. Tenants change; plates don't.

### Locations without a plate
Staff call these by fixed names. They span several floors, so the user must also pick a floor.

| Code | Name (Persian, as staff say it) | English |
|---|---|---|
| `PL-SAF` | پله اضطراری صفرخانی | Emergency stair — Safarkhani |
| `PL-ASD` | پله اضطراری اسدنژاد | Emergency stair — Asadnejad |
| `PL-W` | پله اضطراری غربی | Emergency stair — West |
| `AB-SAF` | آسانسور باری صفرخانی | Freight elevator — Safarkhani |
| `AB-ASD` | آسانسور باری اسدنژاد | Freight elevator — Asadnejad |
| `AS-SAF` | آسانسور شیشه‌ای صفرخانی | Glass elevator — Safarkhani |
| `AS-ASD` | آسانسور شیشه‌ای اسدنژاد | Glass elevator — Asadnejad |
| `AD-ASD` | آسانسور دوقلوی اسدنژاد | Twin elevator — Asadnejad |
| `AD-CAF` | آسانسور دوقلوی کافه | Twin elevator — Café |
| `GEN` | مشاعات عمومی طبقه | General common area of a floor (when no plate is nearby) |

*(Safarkhani and Asadnejad are the two alleys next to the building: the main door opens onto Safarkhani, and Asadnejad runs along the far end.)*

### Named places with a fixed floor

| Code | Name | Floor |
|---|---|---|
| `CAFE` | کافه کاسپین (Caspian Café) | Basement |
| `OFFICE` | دفتر مدیریت (Management office) | 3rd |
| `LAMINIK` | غرفه لمینیک (Laminik booth) | 2nd |
| `SUPER` | سوپرمارکت (Supermarket) | P1 |
| `P1`–`P4` | پارکینگ (Parking levels) | itself |
| `BAM` | بام (Roof) | itself |

---

## Part 2 — Implementation spec for Caspian Warehouse

> Run in the `Caspian_Warehouse` repo, **Plan mode first**. Place the CSV at `data/seed/caspian_locations.csv`.
> This **replaces** the "tree-structured consumption location" item in the pending next-phase warehouse prompt (the item covering request approval, owner sign-off and similar). The rest of that prompt stays as is.
> Stack reminder: Windows desktop, Python 3.12, Qt, MariaDB, SQLAlchemy async + Alembic, uv, pytest, ruff. UI is Persian/RTL with Jalali dates and light/dark themes.

### 2.1 Goal
Add a **Consumption Location** (محل مصرف) field to issue vouchers (حواله), and later to stock requests. The user should fill it with **a single entry**. Typing a plate number auto-fills the floor.

### 2.2 Floor codes (stable across all systems)

| Code | Persian | Sort |
|---|---|---|
| `P4` | پارکینگ P4 | 1 |
| `P3` | پارکینگ P3 | 2 |
| `P2` | پارکینگ P2 | 3 |
| `P1` | پارکینگ P1 | 4 |
| `B` | زیرهمکف | 5 |
| `G` | همکف | 6 |
| `F1` | اول | 7 |
| `F2` | دوم | 8 |
| `F3` | سوم | 9 |
| `ROOF` | بام | 10 |

### 2.3 CSV format
Columns: `code, name, type, floor, floor_code, needs_floor, aliases, merged_with, note`

| `type` | Meaning | Floor behaviour |
|---|---|---|
| `plate` | Shop plate | Fixed, auto-filled |
| `named` | Named place (café, office, booth, supermarket) | Fixed |
| `vertical` | Emergency stair / elevator | **User must pick a floor** |
| `general` | "General common area of floor" | **User must pick a floor** |
| `floor_place` | Parking level / roof | The location *is* the floor |

- `aliases`: separated by `;`. These are the names staff actually say, and they are used for search.
- `merged_with`: plates that form one shop, comma-separated. Informational only.

### 2.4 Data model
- **`floors`**: `id, code (unique), name_fa, sort_order, is_active`
- **`locations`**: `id, code (unique), name_fa, type, floor_id (nullable), needs_floor (bool), merged_group (nullable text), is_active, note, created_at, updated_at`
- **`location_aliases`**: `id, location_id, alias, alias_normalized` (indexed)
- **On the voucher** (and later the stock request):
  - `location_id` (required)
  - `consumption_floor_id` (required, **always stored on the document**, even for plates)
  - `usage_note` (free text, optional)
  - Storing the floor on the document means later edits to location data **never rewrite history**.
- **Future zones:** do not create a zone column now. Keep the design open so a later Alembic migration can add a nullable `zone_id` to `locations` and documents.
- **Seeding:** an Alembic data migration seeds floors and locations from the CSV. It must be **idempotent**: upsert by `code`, so re-running never duplicates.
- **No deletes:** locations are never deleted, only deactivated. This follows the same rule as warehouse documents.
- **Codes are the cross-system key:** `locations.code` and `floors.code` are what the server and the Owner app use. Never expose internal DB ids outside the warehouse.

### 2.5 Input normalisation
Apply the same function to user input and to `alias_normalized`:
- Persian and Arabic digits → Latin (`۶۸`, `٦٨` → `68`)
- `ي` → `ی`, `ك` → `ک`; strip ZWNJ, tatweel (ـ) and extra spaces; remove spaces inside names so «اسد نژاد» = «اسدنژاد» and «دو قلو» = «دوقلو»
- `126/1`, `126-1` and `۱۲۶/۱` are the same code

### 2.6 UI — reusable `LocationPicker` widget (Qt, RTL)
1. One autocomplete search box that matches `code`, `name` and `aliases`.
2. If the input **exactly** matches a plate (for example `68` then Enter), select it directly without opening the list.
3. After selection, show a summary label under the field, e.g. **«پلاک ۶۸ · همکف»**.
4. If `needs_floor = yes`, show a **floor combo box** next to it. The document cannot be saved until a floor is chosen. Hide the combo for every other type.
5. If a **Phase 1 plate** is typed, show «این پلاک مربوط به فاز ۱ است» and do not accept it.
6. The widget must be fully keyboard-driven (type, arrows, Enter, Tab), so the storekeeper never needs the mouse.
7. Display numbers with Persian digits.

### 2.7 Admin screen — Locations (admin only)
- List with search, filtered by type and floor.
- Add, edit and deactivate locations and their aliases.
- **Re-import CSV with a preview of changes** before applying, following the existing draft-based import flow.
- Excel export.
- Every change goes to the full audit log.
- The aim is that future changes (zones, new staff nicknames, corrections) need **no code changes**.

### 2.8 Reports
- Consumption **by floor** and **by location**, filterable by Jalali date range, item and requester.
- Excel and PDF export, like the other reports.
- The AI assistant and the read-only MCP can answer questions like "consumption on the ground floor this month".

### 2.9 Tests (pytest)
- Normalisation: `۶۸` / `68` / `٦٨` resolve to the same plate; every form of `126/1` resolves; «باری اسد نژاد» → `AB-ASD`.
- A Phase 1 plate (e.g. `50`) is rejected with the right message.
- `vertical` and `general` locations cannot be saved without a floor.
- Running the seed twice creates no duplicates.
- Changing a location's floor does **not** change the floor stored on older vouchers.
- A deactivated location is hidden from search but still appears in historical reports.
- All existing tests (currently 424) still pass.

---

## Part 3 — How this fits Warehouse ↔ Server ↔ Caspian Owner

### Already decided (from earlier sessions)
- **Caspian Owner** (Flutter, Android) talks **only to a middle server**, never to the warehouse database. Until the server exists, it is built against **mock data** and an **OpenAPI contract**.
- The **warehouse database is never exposed to the internet.**
- The warehouse will have an **outbox table** for future syncing to the server.
- The server will be an **Iranian VPS** running Ubuntu. Parspack is the leading option: start on **irVPS4** and upgrade to **irVPS5** later. ArvanCloud and Liara are alternatives. No Google Play Services or Firebase, so everything keeps working on Iran's national internet.
- The owner app needs to show **consumption by location**: on the dashboard, on each request (usage history of the same location) and later on a **live building map**.

### Proposed direction (not built yet — to be done step by step)

```
Caspian Warehouse (LAN, MariaDB)
   │  pushes events from its outbox (outbound HTTPS only, from inside the mall)
   ▼
Middle server (Iranian VPS)
   │  REST API + push notifications to the app
   ▼
Caspian Owner (Android)
```

- **The warehouse is the source of truth** for locations and floors. It pushes a **versioned snapshot** of both to the server whenever they change. The owner app caches that snapshot.
- **The warehouse always initiates the connection** (push, then poll for owner decisions such as approve, reject or start a surprise count). This means no port is opened on the mall network.
- Every event and document carries **`location_code` and `floor_code`**, never internal ids.

### What the location feature needs in the shared API contract
Add these to the OpenAPI contract that Caspian Owner is built against, so the mock data and the real server stay compatible:

```yaml
components:
  schemas:
    Floor:
      type: object
      required: [code, name_fa, sort_order]
      properties:
        code: { type: string, example: "G" }
        name_fa: { type: string, example: "همکف" }
        sort_order: { type: integer, example: 6 }
    Location:
      type: object
      required: [code, name_fa, type, needs_floor, is_active]
      properties:
        code: { type: string, example: "68" }
        name_fa: { type: string, example: "پلاک 68" }
        type: { type: string, enum: [plate, named, vertical, general, floor_place] }
        floor_code: { type: string, nullable: true, example: "G" }
        needs_floor: { type: boolean }
        aliases: { type: array, items: { type: string } }
        is_active: { type: boolean }
    LocationsSnapshot:
      type: object
      required: [version, floors, locations]
      properties:
        version: { type: integer, description: "Increments on every change in the warehouse" }
        floors: { type: array, items: { $ref: "#/components/schemas/Floor" } }
        locations: { type: array, items: { $ref: "#/components/schemas/Location" } }
    ConsumptionPlace:
      description: "Embedded in every request, voucher and consumption event"
      type: object
      required: [location_code, floor_code]
      properties:
        location_code: { type: string, example: "AB-ASD" }
        floor_code: { type: string, example: "F2" }
        usage_note: { type: string, nullable: true }

paths:
  /v1/locations:
    get:
      summary: Current locations snapshot (app caches it; send If-None-Match / version)
  /v1/reports/consumption:
    get:
      summary: Consumption grouped by floor or location
      parameters:
        - { name: group_by, in: query, schema: { type: string, enum: [floor, location] } }
        - { name: from, in: query, schema: { type: string, format: date } }
        - { name: to, in: query, schema: { type: string, format: date } }
```

### Outbox events touching locations
- `locations.snapshot` — the full snapshot, sent when the version changes
- `voucher.issued` — includes a `ConsumptionPlace`
- `request.created` — includes a `ConsumptionPlace` (once the request module exists)

### For the Caspian Owner app
- Add mock data generated from `caspian_locations.csv`.
- Show the location everywhere as **«پلاک ۶۸ · همکف»** or **«آسانسور باری اسدنژاد · طبقه دوم»**.
- Dashboard chart "consumption by floor": the floors are on the y-axis in building order (P4 → roof).
- **Live map (later version):** start with a simple stacked-floors diagram, one bar per floor coloured by consumption. A per-plate floor plan comes only after zones are introduced.

### Server — open items to work through together, step by step
1. Pick and buy the VPS; set up basic hardening (SSH keys, firewall, automatic updates).
2. Choose the server stack. Keep it close to what is already known: Python, so **FastAPI + PostgreSQL or MariaDB** is a natural fit.
3. Implement the OpenAPI contract: auth for owners (device registration and tokens), locations snapshot, requests, approvals and reports.
4. Build the warehouse outbox sender and the decision poller.
5. Swap Caspian Owner from mock data to the real server.
6. Add backups, monitoring, the update server (for app and warehouse updates) and the SMS gateway.

**Suggested order for the next session:** implement Part 2 in the warehouse first. It works with no server. Then add the Part 3 schemas to the Owner app's OpenAPI contract and mocks. Start the server only after both are done.

---

## Reminders carried over
- 🔑 **The Caspian Owner Android keystore has not been created yet.** Create the `.jks` file and store the file, its password and the alias in KeePassXC, with backups in 3 places.
- Never put PINs, passwords or API keys in code, prompts or chat.
