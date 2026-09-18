# Jobhunt

Busqueda de empleo automatizada. Cada manana lee las paginas de carreras de tus
empresas y las alertas de empleo que te llegan por email, puntua cada oferta
nueva contra tu perfil y te la deja en una pagina web donde decides Aplicar o
Descartar. Las aceptadas se convierten en una carpeta de Drive con CV y carta
adaptados, la descripcion del puesto y una fila en tu hoja de seguimiento.

Este proyecto es independiente del resto del repositorio. Para moverlo a su
propio repo basta con copiar esta carpeta y los tres workflows
`.github/workflows/jobhunt_*.yml` (en ellos cambia `working-directory: jobhunt`
por `.`).

## Piezas

| Pieza | Que hace | Donde corre |
| --- | --- | --- |
| `pipeline/scrape.py` | Lee empresas y alertas, deduplica, prefiltra por palabras clave, puntua con Claude | GitHub Actions, diario 05:00 UTC |
| `web/` | Lista de ofertas con encaje, razones, JD desplegable, botones Aplicar y Descartar, dashboard | Vercel (o local) |
| `pipeline/process.py` | Para cada aceptada genera CV y carta, crea la carpeta en Drive, sube la JD, anade la fila al Sheet | GitHub Actions, cada hora |
| `supabase/schema.sql` | Las dos tablas que unen todo (`jobs` y `runs`) | Supabase, plan gratuito |

Estados de una oferta: `new` (rastreada) → `filtered` (no pasa el filtro, oculta)
o `pending` (esperando tu decision) → `accepted` o `discarded` → `processing` →
`ready` (documentos en Drive) → `applied` (la marcas tu). Si algo falla queda
en `error` con el motivo y un boton de reintentar.

## Fuentes

Paginas de empresa, por tipo en `config/companies.yaml`:

- `greenhouse`, `lever`, `smartrecruiters`, `ashby`, `workday`: usan la API JSON
  publica que la propia pagina de carreras consulta. Traen titulo, ubicacion y
  descripcion completa.
- `html`: cualquier otra pagina. Recoge los enlaces que cumplen `link_pattern`.
  Si la pagina pinta las ofertas solo con JavaScript no devuelve nada; para esas
  empresas usa una alerta de empleo o Claude en Chrome.

Alertas por email (`config/searches.yaml`, bloque `alerts`): LinkedIn no tiene
API de ofertas ni conector, asi que la via es crear alertas diarias en LinkedIn,
jobs.ch, jobup.ch o Indeed con tus busquedas y leerlas desde Gmail. El parser
reconoce los enlaces de oferta de esos sitios en cualquier email.

## Puesta en marcha

1. **Perfil y plantillas.** Edita `config/profile.md`, `config/templates/cv.md`,
   `config/templates/cover_letter.md` y `config/writing_rules.md`. El perfil
   manda en la puntuacion, cuanto mas concreto mejor.
2. **Empresas y filtro.** Edita `config/companies.yaml` y `config/searches.yaml`.
3. **Supabase.** Crea un proyecto gratuito, pega `supabase/schema.sql` en el
   SQL editor. Guarda la URL y la `service_role` key.
4. **Google.** En Google Cloud crea un proyecto, activa Gmail API, Drive API y
   Sheets API, crea credenciales OAuth de tipo Desktop y descargalas como
   `client_secret.json` en esta carpeta. Ejecuta `python auth_google.py` una vez;
   genera `token.json`. Crea en Drive la carpeta raiz para las aplicaciones y
   un Sheet para el seguimiento, y copia sus ids de la URL.
5. **Claude.** Una API key de https://console.anthropic.com.
6. **Secrets del repo** (Settings → Secrets and variables → Actions):
   - Secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ANTHROPIC_API_KEY`,
     `GOOGLE_TOKEN_JSON` (el contenido de `token.json`).
   - Variables: `DRIVE_ROOT_FOLDER_ID`, `TRACKER_SHEET_ID`, opcional
     `JOBHUNT_SCORE_LIMIT` (ofertas puntuadas por dia, 60 por defecto).
7. **Web.** Importa `jobhunt/web` en Vercel con las variables `SUPABASE_URL`,
   `SUPABASE_SERVICE_KEY` y `APP_PASSWORD`. Sin Vercel, `npm run dev` en local.
8. Lanza el workflow "Jobhunt daily scrape" a mano la primera vez y abre la web.

## Probarlo sin cuentas

```bash
cd jobhunt
pip install -r requirements.txt
python -m pytest -q                         # tests de parseo, filtro y store
python -m pipeline.scrape --no-score        # rastrea a data/jobs.json sin usar la API
python -m pipeline.scrape                   # con ANTHROPIC_API_KEY puntua de verdad
cd web && npm install && npm run dev        # http://localhost:3000, lee data/jobs.json
python -m pipeline.process --dry-run        # documentos en data/out/ en vez de Drive
```

Sin `data/jobs.json` la web muestra `data/sample_jobs.json`, que trae ocho
ofertas de ejemplo para ver el diseno.

## Coste

Cada oferta que pasa el prefiltro cuesta una llamada corta a Claude (unos
centimos); el limite diario `JOBHUNT_SCORE_LIMIT` acota el gasto. Cada oferta
aceptada cuesta una llamada larga (CV y carta). Supabase, Vercel y GitHub
Actions entran en los planes gratuitos con este volumen.

## Limites conocidos

- LinkedIn no deja leer la descripcion sin sesion. Las ofertas que llegan por
  alerta de LinkedIn se puntuan solo con el titulo y la empresa; la web te lleva
  a la oferta y, si la aceptas, el procesador vuelve a intentar leer la JD.
- Las paginas que cargan ofertas con JavaScript no se leen con el tipo `html`.
- El modelo se fija en `pipeline/llm.py` (`JOBHUNT_MODEL`, por defecto
  `claude-opus-5`).
