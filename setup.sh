#!/usr/bin/env bash
# =============================================================
# RADAR INMOBILIARIO — instalación automática
# Requisitos: GitHub CLI instalado (https://cli.github.com) y
# sesión iniciada con: gh auth login
# Uso: bash setup.sh   (desde dentro de esta carpeta)
# =============================================================
set -e

REPO="radar-inmobiliario"

echo "== Radar Inmobiliario: instalación automática =="

# 0. verificaciones
command -v gh >/dev/null || { echo "ERROR: instala GitHub CLI primero: https://cli.github.com"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "ERROR: inicia sesión primero con: gh auth login"; exit 1; }
OWNER=$(gh api user -q .login)
echo "Cuenta detectada: $OWNER"

# 1. API key de Claude (opcional pero recomendada)
echo ""
echo "Pega tu API key de Anthropic para racionales inteligentes"
echo "(déjala vacía y presiona Enter para usar racionales básicos;"
echo " puedes agregarla después en Settings → Secrets):"
read -r -s ANTHROPIC_KEY
echo ""

# 2. crear repo y subir código
if gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
  echo "El repo $OWNER/$REPO ya existe; subiendo cambios ahí."
else
  gh repo create "$REPO" --public --description "Radar diario de oportunidades inmobiliarias" >/dev/null
  echo "Repo creado: $OWNER/$REPO (público: necesario para la página web gratis)"
fi
rm -rf .git
git init -q -b main
git add -A
git -c user.name="setup" -c user.email="setup@local" commit -q -m "radar inmobiliario: instalación inicial"
git remote add origin "https://github.com/$OWNER/$REPO.git" 2>/dev/null || true
git push -q -u origin main --force
echo "Código subido."

# 3. permisos de escritura para el workflow
gh api -X PUT "repos/$OWNER/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=write >/dev/null
echo "Permisos del workflow configurados."

# 4. secret de Anthropic (si se entregó)
if [ -n "$ANTHROPIC_KEY" ]; then
  printf '%s' "$ANTHROPIC_KEY" | gh secret set ANTHROPIC_API_KEY -R "$OWNER/$REPO"
  echo "Secret ANTHROPIC_API_KEY guardado."
else
  echo "Sin API key: los racionales serán plantilla básica (se puede agregar después)."
fi

# 5. activar GitHub Pages desde /docs
gh api -X POST "repos/$OWNER/$REPO/pages" \
  -f build_type=legacy -f "source[branch]=main" -f "source[path]=/docs" >/dev/null 2>&1 \
  || gh api -X PUT "repos/$OWNER/$REPO/pages" \
       -f build_type=legacy -f "source[branch]=main" -f "source[path]=/docs" >/dev/null 2>&1 \
  || echo "Aviso: no pude activar Pages por API; actívalo a mano en Settings → Pages → main /docs."
echo "Página web activada."

# 6. lanzar la primera corrida (GitHub tarda unos segundos en registrar el workflow)
echo "Lanzando la primera ejecución del pipeline…"
for i in 1 2 3 4 5 6; do
  if gh workflow run daily.yml -R "$OWNER/$REPO" >/dev/null 2>&1; then
    LANZADO=1; break
  fi
  sleep 5
done
if [ -n "$LANZADO" ]; then
  echo "Primera corrida en marcha (demora 2-5 min)."
else
  echo "Aviso: lánzala a mano en la pestaña Actions → 'Radar diario' → Run workflow."
fi

echo ""
echo "================================================================"
echo "  LISTO. Tu reporte diario quedará en:"
echo "  https://$OWNER.github.io/$REPO/"
echo ""
echo "  Desde mañana corre solo todos los días a las 7-8 AM (Chile)."
echo "  Progreso de la primera corrida:"
echo "  https://github.com/$OWNER/$REPO/actions"
echo "================================================================"
