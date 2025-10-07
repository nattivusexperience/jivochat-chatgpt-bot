
# kb/booking

Carpeta orientada a **FAQs operativas de reserva** (no texto legal). Aquí NO se duplican condiciones; los textos legales canónicos viven en `kb/common/legal/` (pagos, cancelaciones, cambios, seguro opcional de anulación, condiciones de transfers).

## Estructura
```
kb/booking/
 ├─ README.md
 ├─ index.jsonl        ← índice temático que referencia IDs canónicos de legal y documentos de booking
 ├─ aliases.json       ← sinónimos ES/EN para mejorar la búsqueda del bot
 └─ faq/
    ├─ how-to-book-es.md
    ├─ how-to-book-en.md
    ├─ how-to-book-es.jsonl
    └─ how-to-book-en.jsonl
```

## Convenciones de `.jsonl`
- **1 registro por sección**.
- Campos recomendados:
  - `id` (slug único)
  - `title` (título corto de la sección)
  - `language` (`es` | `en`)
  - `order` (entero para ordenar secciones)
  - `content` (texto completo, sin recortes)
  - `related_refs` (opcional): lista de IDs de `kb/common/legal/` a los que se relaciona la sección.
  - `applies_to` (opcional): `["all"]` o un tren concreto (p. ej. `["alandalus"]`).

## Enlace con `kb/common/legal/`
- Este directorio solo **explica el “cómo”** (flujo de reserva, métodos de pago aceptados, recordatorios, etc.).
- Cuando haga falta normativa, el bot debe ir a `kb/common/legal/` usando los IDs referenciados en `index.jsonl` o en `related_refs` de cada sección.

## Idiomas
- Por defecto mantenemos ES y EN.
