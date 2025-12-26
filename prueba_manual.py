import json
import os

# 1. Cargamos manualmente la configuración local
# Esto es necesario porque al ejecutar con "python3" directo, 
# no se carga el local.settings.json automáticamente.
try:
    with open('local.settings.json', 'r') as f:
        settings = json.load(f)
        # Extraemos la conexión y la ponemos en las variables de entorno
        conn_str = settings['Values']['STORAGE_CONNECTION']
        os.environ['STORAGE_CONNECTION'] = conn_str
        print("✅ Configuración cargada correctamente.")
except FileNotFoundError:
    print("❌ Error: No encuentro el archivo local.settings.json")
    exit()
except KeyError:
    print("❌ Error: No encuentro 'STORAGE_CONNECTION' dentro de local.settings.json")
    exit()

# 2. Ahora sí, importamos tu módulo
# Al importar, gestor_tabla leerá la variable que acabamos de inyectar arriba
try:
    from gestor_tabla import guardar_reunion_procesada, verificar_si_existe
except ImportError as e:
    print(f"❌ Error importando tus funciones: {e}")
    exit()

# 3. Ejecutamos la prueba
print("--- INICIANDO PRUEBA ---")

ID_PRUEBA = "TEST-FEDORA-01"
ASUNTO_PRUEBA = "Prueba desde Terminal Linux"

# A) Verificar si existe (Debería decir False la primera vez)
existe = verificar_si_existe(ID_PRUEBA)
print(f"¿Existe la reunión {ID_PRUEBA}?: {existe}")

if not existe:
    # B) Guardar
    print("Intentando guardar en Azure...")
    guardar_reunion_procesada(ID_PRUEBA, ASUNTO_PRUEBA)
    print("✅ Guardado finalizado.")
else:
    print("⚠️ Ya existía, no la guardo de nuevo.")

print("--- FIN DE PRUEBA ---")