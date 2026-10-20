param([Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
# Built-in Windows PowerShell/.NET only: usable inside a clean Windows Sandbox.
# The fixed key below is public fixture data, never a real provider credential.
$null = New-Item -ItemType Directory -Path $OutputDirectory -Force
$source = @'
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Text.RegularExpressions;

public class BaguAcceptanceMock {
    readonly TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
    readonly string probe, quiz;
    int probes, quizzes, rejected;
    public BaguAcceptanceMock(string probeBody, string quizBody) { probe=probeBody; quiz=quizBody; }
    public int Start() {
        listener.Start();
        new Thread(() => { while(true) { var client=listener.AcceptTcpClient();
            ThreadPool.QueueUserWorkItem(_ => Handle(client)); } }) {IsBackground=true}.Start();
        return ((IPEndPoint)listener.LocalEndpoint).Port;
    }
    void Handle(TcpClient client) {
        using(client) {
            client.ReceiveTimeout=5000; client.SendTimeout=5000;
            try {
                var stream=client.GetStream();
                var header=new StringBuilder();
                while(header.Length < 16384) {
                    int ch=stream.ReadByte(); if(ch<0) return; header.Append((char)ch);
                    if(header.ToString().EndsWith("\r\n\r\n")) break;
                }
                string headers=header.ToString(), response; int status=200;
                if(headers.StartsWith("GET /status ")) {
                    response="{\"probes\":"+probes+",\"quizzes\":"+quizzes+",\"rejected\":"+rejected+"}";
                } else if(!headers.StartsWith("POST /v1/chat/completions ")) {
                    status=404; response="{\"error\":\"wrong endpoint\"}";
                } else {
                    var length=Regex.Match(headers, @"(?im)^Content-Length:\s*(\d+)");
                    int size=length.Success ? Int32.Parse(length.Groups[1].Value) : 0;
                    if(size < 1 || size > 65536) return;
                    byte[] data=new byte[size]; int done=0;
                    while(done < size) { int count=stream.Read(data,done,size-done); if(count==0) return; done+=count; }
                    string body=Encoding.UTF8.GetString(data);
                    if(!Regex.IsMatch(headers,@"(?im)^Authorization:\s*Bearer acceptance-local-only-not-a-secret\r?$")) {
                        Interlocked.Increment(ref rejected); status=401; response="{\"error\":\"fixture authorization failed\"}";
                    } else if(!Regex.IsMatch(body,"\"model\"\\s*:\\s*\"local-model\"")) {
                        Interlocked.Increment(ref rejected); status=404; response="{\"error\":\"fixture model not found\"}";
                    } else if(body.Contains("\"max_tokens\"")) { Interlocked.Increment(ref probes); response=probe; }
                    else { Interlocked.Increment(ref quizzes); response=quiz; }
                }
                byte[] payload=Encoding.UTF8.GetBytes(response);
                byte[] prefix=Encoding.ASCII.GetBytes("HTTP/1.1 "+status+" Result\r\nContent-Type: application/json; charset=utf-8\r\nConnection: close\r\nContent-Length: "+payload.Length+"\r\n\r\n");
                stream.Write(prefix,0,prefix.Length); stream.Write(payload,0,payload.Length);
            } catch(IOException) {} catch(SocketException) {} catch(FormatException) {}
        }
    }
}
'@
Add-Type -TypeDefinition $source
$question = @{
    question = '数据库查询需要减少扫描的数据量，依据来源中的索引原理，应优先采用哪种措施？'
    options = @{A='结合查询条件建立合适索引';B='删除所有索引以加速查询';C='每次查询扫描所有记录';D='仅改变变量命名'}
    answer = 'A'
    analysis = '本地模拟接口固定解析：索引用额外的数据结构缩小查询扫描范围，同时会增加维护成本。'
} | ConvertTo-Json -Depth 6 -Compress
$probe = @{choices=@(@{message=@{content='OK'}})} | ConvertTo-Json -Depth 6 -Compress
$quiz = @{choices=@(@{message=@{content=$question}})} | ConvertTo-Json -Depth 6 -Compress
$server = New-Object BaguAcceptanceMock($probe, $quiz)
$port = $server.Start()
$ready = @{base_url="http://127.0.0.1:$port/v1";model='local-model';fixture_key='acceptance-local-only-not-a-secret';pid=$PID}
$ready | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $OutputDirectory 'mock.json') -Encoding UTF8
while ($true) { Start-Sleep -Seconds 1 }
