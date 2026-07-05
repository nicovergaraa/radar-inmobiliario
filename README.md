# Radar Inmobiliario

Pipeline automático que todos los días revisa los avisos de Portal Inmobiliario (vía API de MercadoLibre), deduplica republicaciones para reconstruir el historial real de cada propiedad, detecta las que están bajo la mediana UF/m² de su zona, genera un racional con Claude y publica el reporte como página web.

## Instalación rápida (un comando)

1. Instala GitHub CLI: https://cli.github.com (si no tienes cuenta GitHub, créala gratis en github.com).
2. Abre un terminal (en Windows: Git Bash, que viene con https://gitforwindows.org), entra a esta carpeta e inicia sesión una vez: `gh auth login`
3. Ejecuta: `bash setup.sh`

El script crea el repositorio, sube el código, configura la página web, los permisos, tu API key y lanza la primera corrida. Al terminar te imprime la URL de tu reporte diario. Eso es todo: desde ahí corre solo cada mañana.

---

## Instalación manual (alternativa, ~15 minutos)

**1. Crear el repositorio.** En GitHub crea un repo nuevo (por ejemplo `radar-inmobiliario`) y sube todos estos archivos manteniendo la estructura. Ojo con la carpeta `.github/` — es oculta; si subes por la web usa "Add file → Upload files" arrastrando la carpeta completa, o usa git desde tu computador.

> Nota de privacidad: en el plan gratuito de GitHub, la página web (Pages) solo funciona en repos **públicos**, es decir tu reporte diario será visible para quien tenga el link. Si quieres que sea privado, necesitas GitHub Pro, o cambia la entrega a otro canal (Telegram/correo) y usa repo privado.

**2. Activar la página web.** En el repo: Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, carpeta `/docs` → Save. Tu reporte quedará en `https://TU_USUARIO.github.io/radar-inmobiliario/`.

**3. Dar permiso de escritura al workflow.** Settings → Actions → General → Workflow permissions → marcar "Read and write permissions" → Save.

**4. (Recomendado) Agregar la API key de Claude para los racionales.** Sin ella el pipeline funciona igual, pero los racionales son una plantilla básica sin lectura de la descripción del aviso ni detección de red flags. Con ella, Claude lee cada descripción y explica la oportunidad y sus riesgos.
   - Crea una key en https://console.anthropic.com (los ~15 racionales diarios cuestan centavos de dólar al día).
   - En el repo: Settings → Secrets and variables → Actions → New repository secret → nombre `ANTHROPIC_API_KEY`, valor tu key.

**5. Primera ejecución.** Pestaña Actions → "Radar diario" → Run workflow. Demora 2-5 minutos. Al terminar, revisa tu página de Pages (puede tardar 1-2 min extra en publicarse).

**Si la ejecución falla con error de autenticación de MercadoLibre:** la API dejó de ser pública para esa consulta. Crea una aplicación gratuita en https://developers.mercadolibre.cl, obtén un access token y agrégalo como secret `ML_ACCESS_TOKEN` (paso 4, mismo procedimiento). El pipeline lo usará automáticamente.

## Uso diario

Nada: corre solo todos los días a las 11:00 UTC (7-8 AM en Chile según horario de verano) y actualiza la página. Tú solo abres el link en la mañana. Para cambiar la hora, edita el `cron` en `.github/workflows/daily.yml` (está en UTC).

Las propiedades ya mostradas se marcan y en días siguientes aparecen sin la etiqueta "nueva en el radar", salvo que bajen aún más de precio.

## Configuración (`config.json`)

| Campo | Qué hace | Por defecto |
|---|---|---|
| `operacion` | `venta` o `arriendo` | venta |
| `comunas` | lista de comunas a considerar; `[]` = todas | todas |
| `paginas` | páginas de 50 avisos a revisar por corrida | 10 |
| `top_n` | oportunidades en el reporte | 50 |
| `min_score` | % mínimo bajo la mediana (0.15 = 15%) | 0.15 |
| `min_comparables` | mínimo de propiedades por zona para confiar en su mediana | 6 |
| `racionales_top` | cuántas oportunidades reciben racional de Claude | 15 |
| `uf_manual` | valor UF de respaldo si mindicador.cl falla | 39500 |

## Cómo funciona

1. **Ingesta**: descarga avisos de la categoría inmuebles Chile y obtiene la UF del día desde mindicador.cl.
2. **Deduplicación**: un aviso se considera republicación de una propiedad existente si coinciden comuna, superficie (±5%) y dormitorios, **y además** tiene la misma foto de portada, el mismo vendedor, o título casi idéntico con precio cercano. Reglas deliberadamente conservadoras: es mejor perder una republicación cruzada entre corredoras que fusionar propiedades distintas y contaminar el historial. Cada propiedad acumula su fecha real de primera aparición, cambios de precio y número de republicaciones.
3. **Modelo de precio**: mediana UF/m² por (comuna, tipo, operación); score = % bajo esa mediana. La base mejora sola: a más días de corridas, más comparables por zona y mejor referencia. Con algunas semanas de datos acumulados en `data/db.json` se puede entrenar un modelo hedónico más fino (siguiente etapa).
4. **Racional**: para las top, Claude lee la descripción completa del aviso y genera 2-3 frases de por qué está barata más los riesgos detectados (sin recepción final, sucesión, ocupada, etc.).
5. **Reporte**: HTML estático en `docs/index.html`, publicado por GitHub Pages. La base (`data/db.json`) se versiona en cada commit, así que tienes historial completo gratis.

## Advertencia

El score compara contra **precios de lista**, no de venta, y lo muy barato casi siempre está barato por algo. Esto es un embudo de candidatos: la verificación en terreno, de títulos y de recepción final sigue siendo tuya.
