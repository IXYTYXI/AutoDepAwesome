<template>
  <div class="community-artwork" aria-hidden="true">
    <svg viewBox="0 0 600 460" fill="none" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient
          id="ow-deep"
          x1="185"
          y1="95"
          x2="360"
          y2="280"
          gradientUnits="userSpaceOnUse"
        >
          <stop stop-color="#4C7BFF" />
          <stop offset=".6" stop-color="#1756ED" />
          <stop offset="1" stop-color="#103CB7" />
        </linearGradient>
        <linearGradient
          id="ow-sky"
          x1="240"
          y1="170"
          x2="430"
          y2="340"
          gradientUnits="userSpaceOnUse"
        >
          <stop stop-color="#A7EBFF" />
          <stop offset=".5" stop-color="#38BCF5" />
          <stop offset="1" stop-color="#1891EB" />
        </linearGradient>
        <linearGradient
          id="ow-white"
          x1="160"
          y1="170"
          x2="380"
          y2="360"
          gradientUnits="userSpaceOnUse"
        >
          <stop stop-color="white" />
          <stop offset=".7" stop-color="#F1F9FF" />
          <stop offset="1" stop-color="#C4DDF9" />
        </linearGradient>
        <radialGradient id="ow-halo">
          <stop stop-color="#81BBFF" stop-opacity=".42" />
          <stop offset="1" stop-color="#A6D9FF" stop-opacity="0" />
        </radialGradient>
        <filter id="ow-shadow" x="-50%" y="-50%" width="200%" height="220%">
          <feDropShadow
            dx="0"
            dy="18"
            stdDeviation="14"
            flood-color="#2555A5"
            flood-opacity=".18"
          />
        </filter>
      </defs>
      <ellipse cx="308" cy="254" rx="263" ry="199" fill="url(#ow-halo)" />
      <g stroke="#387DE9" stroke-opacity=".17">
        <ellipse
          cx="306"
          cy="288"
          rx="255"
          ry="112"
          transform="rotate(-19 306 288)"
        />
        <ellipse
          cx="306"
          cy="288"
          rx="226"
          ry="98"
          transform="rotate(-19 306 288)"
        />
        <path d="M45 338L559 160M108 397L491 113" stroke-dasharray="3 7" />
      </g>
      <g filter="url(#ow-shadow)">
        <path
          d="M166 209C154 109 213 53 286 76C351 96 371 160 342 222L166 209Z"
          fill="url(#ow-deep)"
        />
        <path
          d="M176 203C166 119 216 66 284 86"
          stroke="white"
          stroke-opacity=".45"
          stroke-width="2"
        />
        <path
          d="M308 174C406 143 471 211 444 288C422 351 357 370 291 341L308 174Z"
          fill="url(#ow-sky)"
        />
        <path
          d="M318 182C405 154 459 214 436 282"
          stroke="white"
          stroke-opacity=".65"
          stroke-width="2"
        />
        <path
          d="M357 300C306 389 218 390 176 319C139 257 163 195 222 169L357 300Z"
          fill="url(#ow-white)"
        />
        <path
          d="M347 300C301 376 225 379 185 315"
          stroke="white"
          stroke-width="2"
        />
      </g>
      <g fill="#2972E8">
        <circle cx="79" cy="313" r="6" />
        <circle cx="510" cy="205" r="4" />
        <circle cx="456" cy="93" r="3" />
      </g>
      <g stroke="#2972E8" stroke-width="1.5">
        <path d="M116 120V140M106 130H126M466 355V371M458 363H474" />
      </g>
      <circle
        cx="503"
        cy="303"
        r="21"
        fill="white"
        fill-opacity=".65"
        stroke="white"
      />
      <path
        d="M496 310L510 296M497 296H510V309"
        stroke="#2563EB"
        stroke-width="1.7"
      />
      <g
        fill="#4E78AD"
        font-family="Arial,sans-serif"
        font-size="9"
        letter-spacing="2"
      >
        <text x="385" y="402">IDEAS IN MOTION</text>
        <text x="74" y="89">OWS / CONNECT</text>
      </g>
    </svg>
  </div>
</template>
