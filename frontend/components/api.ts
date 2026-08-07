const API=process.env.NEXT_PUBLIC_API_URL||'http://localhost:8000/api'
export async function getJSON(path:string){const r=await fetch(API+path,{cache:'no-store'});if(!r.ok)throw new Error(await r.text());return r.json()}
export async function postJSON(path:string,body:any){const r=await fetch(API+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error(await r.text());return r.json()}
