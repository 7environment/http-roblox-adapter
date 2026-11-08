local HttpService = game:GetService("HttpService")

local UniversalHttp = {}
UniversalHttp.__index = UniversalHttp

local function failure(message, transport, proxied)
    return {
        Success = false,
        StatusCode = 0,
        StatusMessage = nil,
        Headers = {},
        Body = "",
        BodyEncoding = "text",
        Error = message,
        Transport = transport,
        Proxied = proxied == true,
    }
end

local function getEnvironment()
    if type(getgenv) == "function" then
        local ok, environment = pcall(getgenv)
        if ok and type(environment) == "table" then
            return environment
        end
    end
    return _G
end

local function addFullTransport(transports, seen, name, callback)
    if type(callback) == "function" and not seen[callback] then
        seen[callback] = true
        table.insert(transports, {
            Name = name,
            Kind = "full",
            Call = callback,
        })
    end
end

local function findTransports(customTransport)
    local transports = {}
    local seen = {}

    if type(customTransport) == "function" then
        addFullTransport(transports, seen, "custom", customTransport)
    end

    local environment = getEnvironment()
    addFullTransport(transports, seen, "request", environment.request)
    addFullTransport(transports, seen, "http_request", environment.http_request)

    if type(environment.syn) == "table" then
        addFullTransport(transports, seen, "syn.request", environment.syn.request)
    end
    if type(environment.http) == "table" then
        addFullTransport(transports, seen, "http.request", environment.http.request)
    end
    if type(environment.fluxus) == "table" then
        addFullTransport(transports, seen, "fluxus.request", environment.fluxus.request)
    end
    if type(environment.krnl) == "table" then
        addFullTransport(transports, seen, "krnl.request", environment.krnl.request)
    end
    if type(environment.delta) == "table" then
        addFullTransport(transports, seen, "delta.request", environment.delta.request)
    end
    if type(environment.electron) == "table" then
        addFullTransport(transports, seen, "electron.request", environment.electron.request)
    end

    -- These methods are intentionally used instead of HttpService:RequestAsync.
    table.insert(transports, {
        Name = "game:HttpPost",
        Kind = "post",
        Call = function(url, body)
            return game:HttpPost(url, body)
        end,
    })
    table.insert(transports, {
        Name = "game:HttpGet",
        Kind = "get",
        Call = function(url)
            return game:HttpGet(url)
        end,
    })

    return transports
end

local function normalizeFullResponse(raw, transportName, proxied)
    if type(raw) == "string" then
        return {
            Success = true,
            StatusCode = 200,
            StatusMessage = "OK",
            Headers = {},
            Body = raw,
            BodyEncoding = "text",
            Error = nil,
            Transport = transportName,
            Proxied = proxied,
        }
    end

    if type(raw) ~= "table" then
        return failure("HTTP transport returned an unsupported response", transportName, proxied)
    end

    local statusCode = tonumber(raw.StatusCode or raw.Status or raw.status_code) or 0
    local success = raw.Success
    if type(success) ~= "boolean" then
        success = statusCode >= 200 and statusCode < 300
    end

    return {
        Success = success,
        StatusCode = statusCode,
        StatusMessage = raw.StatusMessage or raw.Message,
        Headers = raw.Headers or raw.headers or {},
        Body = raw.Body or raw.body or "",
        BodyEncoding = "text",
        Error = nil,
        Transport = transportName,
        Proxied = proxied,
    }
end

local function normalizeOptions(options)
    if type(options) ~= "table" then
        return nil, "request options must be a table"
    end

    local url = options.Url or options.url
    if type(url) ~= "string" or url == "" then
        return nil, "request Url must be a non-empty string"
    end

    local method = options.Method or options.method or "GET"
    if type(method) ~= "string" then
        return nil, "request Method must be a string"
    end
    method = string.upper(method)
    local headers = options.Headers or options.headers or {}
    if type(headers) ~= "table" then
        return nil, "request Headers must be a table"
    end

    local body = options.Body
    if body == nil then
        body = options.body
    end
    if body ~= nil and type(body) ~= "string" then
        return nil, "request Body must be a string or nil"
    end

    local useProxy = options.UseProxy
    if useProxy == nil then
        useProxy = options.useProxy
    end

    return {
        Url = url,
        Method = method,
        Headers = headers,
        Body = body,
        Timeout = tonumber(options.Timeout or options.timeout),
        FollowRedirects = options.FollowRedirects == true or options.followRedirects == true,
        UseProxy = useProxy,
    }
end

local function decodeProxyResponse(response)
    local ok, decoded = pcall(HttpService.JSONDecode, HttpService, response.Body)
    if not ok or type(decoded) ~= "table" then
        return failure("Proxy returned invalid JSON", response.Transport, true)
    end

    return {
        Success = decoded.success == true,
        StatusCode = tonumber(decoded.statusCode) or 0,
        StatusMessage = decoded.statusMessage,
        Headers = decoded.headers or {},
        Body = decoded.body or "",
        BodyEncoding = decoded.bodyEncoding or "text",
        Error = decoded.error,
        Transport = response.Transport,
        Proxied = true,
    }
end

function UniversalHttp.new(config)
    config = config or {}

    local proxy = config.Proxy or config.proxy or {}
    local proxyUrl = proxy.Url or proxy.url
    if type(proxyUrl) == "string" then
        proxyUrl = string.gsub(proxyUrl, "/+$", "")
    end

    return setmetatable({
        _transports = findTransports(config.Transport or config.transport),
        _proxyEnabled = proxy.Enabled == true or proxy.enabled == true,
        _proxyUrl = proxyUrl,
        _proxyToken = proxy.Token or proxy.token,
        _defaultTimeout = tonumber(config.DefaultTimeout or config.defaultTimeout) or 30,
    }, UniversalHttp)
end

function UniversalHttp:_bestTransport(forProxy, method, hasHeaders, hasBody)
    for _, transport in ipairs(self._transports) do
        if transport.Kind == "full" then
            return transport
        end
        if forProxy and transport.Kind == "post" then
            return transport
        end
        if not forProxy
            and transport.Kind == "get"
            and method == "GET"
            and not hasHeaders
            and not hasBody
        then
            return transport
        end
        if not forProxy and transport.Kind == "post" and method == "POST" and not hasHeaders then
            return transport
        end
    end
    return nil
end

function UniversalHttp:_callFull(transport, options, proxied)
    local ok, raw = pcall(transport.Call, {
        Url = options.Url,
        Method = options.Method,
        Headers = options.Headers,
        Body = options.Body,
    })
    if not ok then
        return failure(tostring(raw), transport.Name, proxied)
    end
    return normalizeFullResponse(raw, transport.Name, proxied)
end

function UniversalHttp:_requestDirect(options)
    local hasHeaders = next(options.Headers) ~= nil
    local transport = self:_bestTransport(false, options.Method, hasHeaders, options.Body ~= nil)
    if not transport then
        return failure(
            "No available transport supports this direct request; enable the proxy",
            nil,
            false
        )
    end

    if transport.Kind == "full" then
        return self:_callFull(transport, options, false)
    end

    local ok, body = pcall(transport.Call, options.Url, options.Body or "")
    if not ok then
        return failure(tostring(body), transport.Name, false)
    end

    return normalizeFullResponse(body, transport.Name, false)
end

function UniversalHttp:_requestThroughProxy(options)
    if type(self._proxyUrl) ~= "string" or self._proxyUrl == "" then
        return failure("Proxy.Url is required when the proxy is enabled", nil, true)
    end

    local transport = self:_bestTransport(true, "POST", false, true)
    if not transport then
        return failure("No POST-capable transport is available for the proxy", nil, true)
    end

    -- HttpService encodes an empty Luau table as JSON `[]`. Omitting empty
    -- headers lets the proxy apply its `{}` default instead of receiving an
    -- array where an object is required.
    local proxyRequest = {
        url = options.Url,
        method = options.Method,
        body = options.Body,
        timeout = options.Timeout or self._defaultTimeout,
        followRedirects = options.FollowRedirects,
    }
    if next(options.Headers) ~= nil then
        proxyRequest.headers = options.Headers
    end

    local encoded, payload = pcall(HttpService.JSONEncode, HttpService, {
        version = 1,
        token = self._proxyToken,
        request = proxyRequest,
    })
    if not encoded then
        return failure("Could not encode proxy payload: " .. tostring(payload), transport.Name, true)
    end

    local response
    if transport.Kind == "full" then
        response = self:_callFull(transport, {
            Url = self._proxyUrl .. "/proxy",
            Method = "POST",
            Headers = {
                ["Content-Type"] = "application/json",
                ["Accept"] = "application/json",
            },
            Body = payload,
        }, true)
    else
        local ok, raw = pcall(transport.Call, self._proxyUrl .. "/proxy", payload)
        if not ok then
            return failure(tostring(raw), transport.Name, true)
        end
        response = normalizeFullResponse(raw, transport.Name, true)
    end

    if response.Error then
        return response
    end
    return decodeProxyResponse(response)
end

function UniversalHttp:Request(options)
    local normalized, validationError = normalizeOptions(options)
    if not normalized then
        return failure(validationError, nil, false)
    end

    local useProxy = self._proxyEnabled
    if normalized.UseProxy ~= nil then
        useProxy = normalized.UseProxy == true
    end

    if useProxy then
        return self:_requestThroughProxy(normalized)
    end
    return self:_requestDirect(normalized)
end

UniversalHttp.request = UniversalHttp.Request

return UniversalHttp
