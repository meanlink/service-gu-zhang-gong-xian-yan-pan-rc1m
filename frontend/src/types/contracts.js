/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean,event_count:number,attempt:number,replayed?:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/** @typedef {{quota_date:string,daily_limit:number,reserved:number,settled:number,released:number,used:number,remaining:number,details:Array<{ledger_id:number,job_id:number,job_name:string,status:string,job_status:string,event_count:number,consumed:number,quota_date:string}>}} QuotaDashboard */
export {};
