import os
import json
from azure.data.tables import TableClient

# Cargar configuración local
try:
    with open('local.settings.json', 'r') as f:
        settings = json.load(f)
        conn_str = settings['Values']['STORAGE_CONNECTION']
except Exception as e:
    print("Error leyendo local.settings.json")
    exit()

table_client = TableClient.from_connection_string(conn_str, table_name="ReunionesProcesadas")

print("--- LIMPIANDO HISTORIAL DE PRUEBAS ---")
# Listar todas las entidades
entidades = table_client.list_entities()
count = 0

for entidad in entidades:
    print(f"Borrando: {entidad['Asunto']} (ID: {entidad['RowKey'][:10]}...)")
    table_client.delete_entity(partition_key=entidad['PartitionKey'], row_key=entidad['RowKey'])
    count += 1

print(f"✅ Se eliminaron {count} registros. Ahora el sistema volverá a procesar todo.")