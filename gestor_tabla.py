import os
from azure.data.tables import TableClient
from azure.core.exceptions import ResourceNotFoundError

# 1. Recuperamos la contraseña que pusimos en local.settings.json
connection_string = os.environ.get("STORAGE_CONNECTION")
table_name = "ReunionesProcesadas"

def obtener_cliente():
    """Conecta con la tabla en la nube"""
    return TableClient.from_connection_string(conn_str=connection_string, table_name=table_name)

def verificar_si_existe(meeting_id):
    """
    Pregunta a la tabla: ¿Ya tienes este ID guardado?
    Retorna True si existe, False si es nueva.
    """
    client = obtener_cliente()
    try:
        # En Azure Tables, necesitamos dos claves para buscar.
        # PartitionKey: Usaremos "comite" como categoría general.
        # RowKey: Usaremos el ID único de la reunión.
        client.get_entity(partition_key="comite", row_key=meeting_id)
        print(f"La reunión {meeting_id} ya fue procesada antes.")
        return True
    except ResourceNotFoundError:
        # Si da error "No encontrado", significa que es nueva.
        return False

def guardar_reunion_procesada(meeting_id, asunto_reunion):
    """
    Escribe una fila nueva en la tabla para que no la procesemos de nuevo.
    """
    client = obtener_cliente()
    
    # Creamos el diccionario con los datos a guardar
    fila_nueva = {
        "PartitionKey": "comite",   # Categoría fija
        "RowKey": meeting_id,       # El ID único (IMPORTANTE)
        "Asunto": asunto_reunion,   # Guardamos el nombre solo por referencia
        "Procesada": True
    }
    
    client.create_entity(entity=fila_nueva)
    print(f"Reunión {meeting_id} guardada en el historial.")