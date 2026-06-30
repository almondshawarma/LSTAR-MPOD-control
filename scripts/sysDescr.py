import asyncio
from puresnmp import V2C, Client
from x690.types import ObjectIdentifier

async def check():
    c = Client(input("Enter the host IP: \n"), V2C("public"))
    r = await c.get(ObjectIdentifier("1.3.6.1.2.1.1.1.0"))
    print(r.value)

asyncio.run(check())