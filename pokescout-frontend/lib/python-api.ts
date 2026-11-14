let csrfToken='';

type Envelope<T>={data:T;error?:{code:string;message:string};request_id:string};

async function parse<T>(response:Response):Promise<T>{
 let envelope:Envelope<T>;
 try{envelope=await response.json()}catch{throw Error('Python API unavailable. Start python3 -m backend --allow-origin http://127.0.0.1:3000.');}
 if(!response.ok)throw Error(envelope.error?.message||`Python API HTTP ${response.status}`);
 return envelope.data;
}

export async function session(){
 const response=await fetch('/api/python/session',{credentials:'same-origin',cache:'no-store'});
 const data=await parse<{csrf_token:string;mode:string}>(response);
 csrfToken=data.csrf_token;
 return data;
}

export async function apiGet<T>(path:string):Promise<T>{
 if(!csrfToken)await session();
 return parse<T>(await fetch('/api/python'+path,{credentials:'same-origin',cache:'no-store'}));
}

export async function apiPost<T>(path:string,body:unknown,revision?:number):Promise<T>{
 if(!csrfToken)await session();
 const headers:Record<string,string>={'Content-Type':'application/json','X-CSRF-Token':csrfToken,'Idempotency-Key':crypto.randomUUID()};
 if(revision!==undefined)headers['If-Match']=`"${revision}"`;
 return parse<T>(await fetch('/api/python'+path,{method:'POST',headers,body:JSON.stringify(body),credentials:'same-origin',cache:'no-store'}));
}

export async function apiUpload(file:File){
 if(!csrfToken)await session();
 const body=new FormData();body.append('manifest',JSON.stringify([{role:'front'}]));body.append('files',file);
 return parse<{upload_id:string}>(await fetch('/api/python/uploads',{method:'POST',body,credentials:'same-origin',headers:{'X-CSRF-Token':csrfToken,'Idempotency-Key':crypto.randomUUID()}}));
}
