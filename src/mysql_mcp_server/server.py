import asyncio
import logging
import os
from sqlalchemy import create_engine as sqlalchemy_create_engine
from mcp.server import Server
from mcp.types import Resource, Tool, TextContent
from pydantic import AnyUrl
from mysql.connector import Error

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mysql_mcp_server")


def create_engine(config):
    MYSQL_URL_SERJ = f"mysql+mysqlconnector://{config['user']}:{config['password']}@{config['host']}:{config['port']}/{config['database']}"
    return sqlalchemy_create_engine(
        MYSQL_URL_SERJ,
        connect_args={
            "ssl_ca": config["ssl_ca"],
            "ssl_verify_cert": True
        } if "ssl_ca" in config else {}
    )

def get_db_config():
    """Get database configuration from environment variables."""
    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
        "ssl_ca": os.getenv("MYSQL_SSL_CA")
    }
    
    if not all([config["user"], config["password"], config["database"]]):
        logger.error("Missing required database configuration. Please check environment variables:")
        logger.error("MYSQL_USER, MYSQL_PASSWORD, and MYSQL_DATABASE are required")
        raise ValueError("Missing required database configuration")
    
    # Configure SSL if certificate is provided
    if config["ssl_ca"]:
        # For DigitalOcean and other managed MySQL services, we need to set these SSL options
        if os.path.exists(config["ssl_ca"]):
            logger.info(f"Using SSL/TLS with CA certificate: {config['ssl_ca']}")
            config["ssl_verify_cert"] = True
            config["ssl_verify_identity"] = True
        else:
            logger.error(f"SSL CA certificate file not found: {config['ssl_ca']}")
            raise ValueError(f"SSL CA certificate file not found: {config['ssl_ca']}")
    else:
        config.pop("ssl_ca", None)
    
    return config

# Initialize server
app = Server("mysql_mcp_server")

@app.list_resources()
async def list_resources() -> list[Resource]:
    """List MySQL tables as resources."""
    config = get_db_config()
    try:
        engine = create_engine(config)
        with engine.raw_connection().cursor() as cursor:
            cursor.execute("SHOW TABLES")
            tables = cursor.fetchall()
            logger.info(f"Found tables: {tables}")
            
            resources = []
            for table in tables:
                resources.append(
                    Resource(
                        uri=f"mysql://{table[0]}/data",
                        name=f"Table: {table[0]}",
                        mimeType="text/plain",
                        description=f"Data in table: {table[0]}"
                    )
                )
            return resources
    except Error as e:
        logger.error(f"Failed to list resources: {str(e)}")
        return []

@app.read_resource()
async def read_resource(uri: AnyUrl) -> str:
    """Read table contents."""
    config = get_db_config()
    uri_str = str(uri)
    logger.info(f"Reading resource: {uri_str}")
    
    if not uri_str.startswith("mysql://"):
        raise ValueError(f"Invalid URI scheme: {uri_str}")
        
    parts = uri_str[8:].split('/')
    table = parts[0]
    
    try:
        engine = create_engine(config)
        conn = engine.raw_connection()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(f"SELECT * FROM {table} LIMIT 100")
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                result = [",".join(map(str, row)) for row in rows]
                return "\n".join([",".join(columns)] + result)
            finally:
                cursor.close()
        finally:
            conn.close()
                
    except Exception as e:
        logger.error(f"Database error reading resource {uri}: {str(e)}")
        raise RuntimeError(f"Database error: {str(e)}")

@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available MySQL tools."""
    logger.info("Listing tools...")
    return [
        Tool(
            name="execute_sql",
            description="Execute an SQL query on the MySQL server",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL query to execute"
                    }
                },
                "required": ["query"]
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute SQL commands."""
    config = get_db_config()
    logger.info(f"Calling tool: {name} with arguments: {arguments}")
    
    if name != "execute_sql":
        raise ValueError(f"Unknown tool: {name}")
    
    query = arguments.get("query")
    if not query:
        raise ValueError("Query is required")
    
    try:
        engine = create_engine(config)
        conn = engine.raw_connection()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(query)
                
                # Special handling for SHOW TABLES
                if query.strip().upper().startswith("SHOW TABLES"):
                    tables = cursor.fetchall()
                    result = ["Tables_in_" + config["database"]]  # Header
                    result.extend([table[0] for table in tables])
                    return [TextContent(type="text", text="\n".join(result))]
                
                # Regular SELECT queries
                elif query.strip().upper().startswith("SELECT"):
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    result = [",".join(map(str, row)) for row in rows]
                    return [TextContent(type="text", text="\n".join([",".join(columns)] + result))]
                
                # Non-SELECT queries
                else:
                    conn.commit()
                    return [TextContent(type="text", text=f"Query executed successfully. Rows affected: {cursor.rowcount}")]
            finally:
                cursor.close()
        finally:
            conn.close()
                
    except Exception as e:
        logger.error(f"Error executing SQL '{query}': {e}")
        return [TextContent(type="text", text=f"Error executing query: {str(e)}")]

async def main():
    """Main entry point to run the MCP server."""
    from mcp.server.stdio import stdio_server
    
    logger.info("Starting MySQL MCP server...")
    config = get_db_config()
    logger.info(f"Database config: {config['host']}/{config['database']} as {config['user']}")
    
    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options()
            )
        except Exception as e:
            logger.error(f"Server error: {str(e)}", exc_info=True)
            raise

if __name__ == "__main__":
    asyncio.run(main())