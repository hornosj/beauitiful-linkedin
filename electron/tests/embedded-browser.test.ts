import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const cookieGet = vi.fn()
const cookieSet = vi.fn()
const cookieRemove = vi.fn()
const setUserAgent = vi.fn()
const setPermissionRequestHandler = vi.fn()
const viewLoadURL = vi.fn()
const viewFocus = vi.fn()
const viewOn = vi.fn()
const viewOnce = vi.fn()
const viewRemoveListener = vi.fn()
const viewGetURL = vi.fn()
const viewIsLoading = vi.fn()
const viewStop = vi.fn()
const viewExecuteJavaScript = vi.fn()
const viewClose = vi.fn()
const viewIsDestroyed = vi.fn()
const viewSetBackgroundThrottling = vi.fn()
const viewSetBounds = vi.fn()
const addChildView = vi.fn()
const removeChildView = vi.fn()
const mainFocus = vi.fn()

const windowLoadURL = vi.fn()
const windowFocus = vi.fn()
const windowShow = vi.fn()
const windowMoveTop = vi.fn()
const windowRestore = vi.fn()
const windowIsMinimized = vi.fn(() => false)
const windowOn = vi.fn()
const windowOnce = vi.fn()
const windowSetWindowOpenHandler = vi.fn()
const windowIsDestroyed = vi.fn()
const windowClose = vi.fn()
const windowWebContentsOn = vi.fn()
const windowWebContentsOnce = vi.fn()
const windowWebContentsFocus = vi.fn()
const windowWebContentsGetURL = vi.fn()
const windowWebContentsExecuteJavaScript = vi.fn()

const browserWindowCtor = vi.fn().mockImplementation(() => ({
  loadURL: windowLoadURL,
  focus: windowFocus,
  show: windowShow,
  moveTop: windowMoveTop,
  restore: windowRestore,
  isMinimized: windowIsMinimized,
  on: windowOn,
  once: windowOnce,
  setWindowOpenHandler: windowSetWindowOpenHandler,
  isDestroyed: windowIsDestroyed,
  close: windowClose,
  webContents: {
    on: windowWebContentsOn,
    once: windowWebContentsOnce,
    setWindowOpenHandler: windowSetWindowOpenHandler,
    focus: windowWebContentsFocus,
    getURL: windowWebContentsGetURL,
    executeJavaScript: windowWebContentsExecuteJavaScript,
    isDestroyed: () => false
  }
}))

vi.mock('electron', () => ({
  BrowserWindow: browserWindowCtor,
  WebContentsView: vi.fn().mockImplementation(() => ({
    webContents: {
      on: viewOn,
      once: viewOnce,
      removeListener: viewRemoveListener,
      loadURL: viewLoadURL,
      getURL: viewGetURL,
      isLoading: viewIsLoading,
      stop: viewStop,
      focus: viewFocus,
      executeJavaScript: viewExecuteJavaScript,
      close: viewClose,
      isDestroyed: viewIsDestroyed,
      setBackgroundThrottling: viewSetBackgroundThrottling
    },
    setBounds: viewSetBounds
  })),
  session: {
    fromPartition: vi.fn(() => ({
      setUserAgent,
      setPermissionRequestHandler,
      cookies: {
        get: cookieGet,
        set: cookieSet,
        remove: cookieRemove
      }
    }))
  }
}))

describe('EmbeddedBrowserManager login window', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    cookieGet.mockResolvedValue([])
    cookieSet.mockResolvedValue(undefined)
    cookieRemove.mockResolvedValue(undefined)
    viewLoadURL.mockResolvedValue(undefined)
    viewGetURL.mockReturnValue('https://www.linkedin.com/login')
    viewIsLoading.mockReturnValue(false)
    viewIsDestroyed.mockReturnValue(false)
    viewExecuteJavaScript.mockResolvedValue(undefined)
    windowIsDestroyed.mockReturnValue(false)
    windowWebContentsGetURL.mockReturnValue('https://www.linkedin.com/login')
    windowWebContentsExecuteJavaScript.mockResolvedValue(undefined)
    windowLoadURL.mockResolvedValue(undefined)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('opens manual LinkedIn login in a separate BrowserWindow sharing the app session', async () => {
    const { EmbeddedBrowserManager } = await import('../src/main/embedded-browser')
    const manager = new EmbeddedBrowserManager()
    manager.attach({
      contentView: { addChildView, removeChildView },
      getContentBounds: () => ({ width: 1280, height: 820 }),
      focus: mainFocus
    } as never)

    const pending = manager.openLogin()
    await vi.runAllTimersAsync()
    const result = await pending

    expect(result).toMatchObject({ ready: true, url: 'https://www.linkedin.com/login' })
    expect(browserWindowCtor).toHaveBeenCalledWith(
      expect.objectContaining({
        width: 1120,
        height: 760,
        webPreferences: expect.objectContaining({
          nodeIntegration: false,
          contextIsolation: true,
          sandbox: true
        })
      })
    )
    expect(windowLoadURL).toHaveBeenCalledWith('https://www.linkedin.com/login')
    expect(addChildView).not.toHaveBeenCalled()
  })
})
